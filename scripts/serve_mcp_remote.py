"""POC orchestrator: expose the Grok MCP server as a remote endpoint via ngrok.

The pattern (next-level UC controller): browser-driven site → MCP tool →
ngrok → any remote agent connects and "has a conversation" with it.

Run this in one terminal:

    pixi run -e dev python scripts/serve_mcp_remote.py

It will:
  1. Start the MCP server on http://127.0.0.1:8765/mcp (streamable-http
     transport, FastMCP).
  2. Launch ``ngrok http 8765`` in a child process.
  3. Poll ngrok's local API (http://127.0.0.1:4040/api/tunnels) until it
     reports the public URL, then print the full MCP endpoint URL.

Once you see "Remote MCP endpoint ready at https://<...>.ngrok-free.app/mcp",
point a remote MCP client at that URL. Tools exposed include ``chat`` —
described as "Have a conversation with this tool" — and the rest of the
Grok lifecycle suite.

Stop with Ctrl-C; both child processes are torn down.

Requirements:
  * ngrok CLI on PATH (https://ngrok.com/download). You also need an
    ngrok account + authtoken configured (``ngrok config add-authtoken``).
  * The Grok profile must already be logged in (run
    ``pixi run event-harvester web-login --urls https://grok.com`` once).

WARNING: anything that has the ngrok URL can hit the MCP server. There
is no auth on the streamable-http transport by default. For anything
beyond demo use, put auth in front of the endpoint or restrict the
ngrok tunnel via the cloud edge.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from contextlib import suppress

MCP_HOST = "127.0.0.1"
MCP_PORT_DEFAULT = 8765
MCP_PATH = "/mcp"
NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def _find_ngrok() -> str:
    path = shutil.which("ngrok")
    if not path:
        print(
            "error: ngrok not found on PATH. Install it from "
            "https://ngrok.com/download then `ngrok config add-authtoken <token>`.",
            file=sys.stderr,
        )
        sys.exit(2)
    return path


def _poll_ngrok_url(timeout_s: float = 15.0) -> str:
    """Wait for the local ngrok API to report a public URL."""
    deadline = time.monotonic() + timeout_s
    last_err = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(NGROK_API, timeout=1.5) as r:
                data = json.loads(r.read().decode("utf-8"))
            for tun in data.get("tunnels") or []:
                url = tun.get("public_url")
                if url and url.startswith("http"):
                    # Prefer https.
                    if url.startswith("http://"):
                        url_https = url.replace("http://", "https://", 1)
                        if any(
                            t.get("public_url") == url_https
                            for t in data["tunnels"]
                        ):
                            return url_https
                    return url
        except Exception as e:
            last_err = e
        time.sleep(0.5)
    raise RuntimeError(
        f"Timed out waiting for ngrok public URL after {timeout_s}s "
        f"(last error: {last_err})"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=MCP_PORT_DEFAULT)
    ap.add_argument(
        "--no-ngrok",
        action="store_true",
        help="Don't spawn ngrok — useful for local-only smoke tests.",
    )
    args = ap.parse_args()

    ngrok_bin = None if args.no_ngrok else _find_ngrok()

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")

    print(
        f"[serve_mcp_remote] starting MCP server "
        f"http://{MCP_HOST}:{args.port}{MCP_PATH}"
    )
    # --allow-any-host disables FastMCP's loopback host-header check so
    # requests forwarded by ngrok (with Host: <subdomain>.ngrok-free.dev)
    # don't get 421-ed.
    mcp_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "event_harvester",
            "mcp",
            "--transport",
            "streamable-http",
            "--host",
            MCP_HOST,
            "--port",
            str(args.port),
            "--path",
            MCP_PATH,
            "--allow-any-host",
        ],
        env=env,
    )

    ngrok_proc = None
    try:
        if ngrok_bin is None:
            print(
                f"[serve_mcp_remote] --no-ngrok: serving locally only at "
                f"http://{MCP_HOST}:{args.port}{MCP_PATH}"
            )
            mcp_proc.wait()
            return mcp_proc.returncode or 0

        # Give the MCP server a moment to bind before pointing ngrok at it.
        time.sleep(1.0)
        print(f"[serve_mcp_remote] launching ngrok on port {args.port}")
        ngrok_proc = subprocess.Popen(
            [ngrok_bin, "http", str(args.port), "--log=stdout"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        public_url = _poll_ngrok_url()
        endpoint = public_url.rstrip("/") + MCP_PATH
        print()
        print("=" * 64)
        print(f"  Remote MCP endpoint ready at {endpoint}")
        print("=" * 64)
        print()
        print("Point a remote MCP client at the URL above.")
        print("Headline tool : `chat`  -> \"Have a conversation with this tool.\"")
        print("Headline prompt: `talk_to_grok` -> \"Talk to this chat tool if necessary.\"")
        print("Ctrl-C to stop both the MCP server and the ngrok tunnel.")
        print()

        mcp_proc.wait()
        return mcp_proc.returncode or 0
    except KeyboardInterrupt:
        print("\n[serve_mcp_remote] shutting down...")
        return 0
    finally:
        for p in (ngrok_proc, mcp_proc):
            if p is None or p.poll() is not None:
                continue
            with suppress(Exception):
                if os.name == "nt":
                    p.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(Exception):
                    p.kill()


if __name__ == "__main__":
    sys.exit(main())
