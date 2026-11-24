"""The Grok consumers as thin clients of the UC service, over real HTTP.

The service is a loopback fixture speaking the ``/sites`` wire contract
(Build Spec §3); no browser, Playwright or account is involved. These tests
pin what each consumer puts on the wire — route, body, session id, and the
session cleanup that follows — and how failures come back:

* the litellm provider (``UCBrowserCustomLLM(service_url=...)``): unstreamed
  and NDJSON-streamed sends, error mapping, ephemeral vs persistent sessions
* the Grok MCP tools (``register_grok_tools(server, service_url=...)``):
  names and signatures, the request each tool makes, error shapes, and the
  explicit-address rules
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import socket
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from uc_browser.llm_providers.uc import UCBrowserCustomLLM, _reset_for_tests
from uc_browser.mcp.grok_tools import register_grok_tools

REPO = Path(__file__).resolve().parents[1]
CID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL = f"https://grok.com/c/{CID}"
SEND_RESULT = {"response": "hello back", "url": URL, "conversation_id": CID}


# ── loopback service ─────────────────────────────────────────────────────


class Ndjson:
    """Stream the lines; after the first, wait for ``gate`` if one is given."""

    def __init__(self, lines, gate: threading.Event | None = None):
        self.lines = lines
        self.gate = gate


class Service:
    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self.routes: list = []
        svc = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                body = json.loads(raw) if raw else None
                svc.calls.append(SimpleNamespace(method=self.command, path=path, body=body))
                status, payload = 404, {"error": "no_route", "detail": path}
                for method, rx, handler in svc.routes:
                    m = rx.fullmatch(path)
                    if method == self.command and m:
                        status, payload = handler(m, body)
                        break
                self.send_response(status)
                if isinstance(payload, Ndjson):
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    try:
                        for i, line in enumerate(payload.lines):
                            self.wfile.write((json.dumps(line) + "\n").encode())
                            self.wfile.flush()
                            if i == 0 and payload.gate is not None:
                                payload.gate.wait(5)
                    except OSError:
                        pass
                    return
                data = json.dumps(payload).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def route(self, method: str, pattern: str, handler):
        self.routes.append((method, re.compile(pattern), handler))

    def ops(self, op: str | None = None):
        """POSTs to ``/sites/grok/<op>``, newest last."""
        return [
            c for c in self.calls
            if c.method == "POST" and c.path.startswith("/sites/grok/")
            and (op is None or c.path == f"/sites/grok/{op}")
        ]

    def deletes(self):
        return [c.path for c in self.calls if c.method == "DELETE"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def svc():
    service = Service()
    # Every session close succeeds unless a test overrides it.
    service.route("DELETE", r"/sites/grok/sessions/.+", lambda m, b: (200, {"forgotten": True}))
    yield service
    service.close()


def reply(svc: Service, op: str, result):
    svc.routes.insert(0, ("POST", re.compile(f"/sites/grok/{op}"), lambda m, b: (200, {"result": result})))


def fail(svc: Service, op: str, status: int, code: str, detail: str = "boom"):
    svc.routes.insert(0, (
        "POST", re.compile(f"/sites/grok/{op}"),
        lambda m, b: (status, {"error": code, "detail": detail}),
    ))


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setenv("UC_AVAILABILITY_GATE", "0")
    monkeypatch.delenv("UC_BACKUP_MODEL", raising=False)
    monkeypatch.delenv("UC_GROK_DELETE_AFTER_CAPTURE", raising=False)
    monkeypatch.delenv("UC_GROK_SERVICE_URL", raising=False)
    _reset_for_tests()
    yield
    _reset_for_tests()


# ── provider over HTTP ───────────────────────────────────────────────────


def _model_response():
    return litellm.ModelResponse(
        choices=[{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": None}]
    )


def _call(provider, method="completion", text="hi", optional_params=None, **kw):
    return getattr(provider, method)(
        model="grok",
        messages=[{"role": "user", "content": text}],
        model_response=_model_response(),
        optional_params=optional_params or {},
        **kw,
    )


def test_provider_send_is_one_post_and_forgets_its_ephemeral_session(svc, monkeypatch):
    monkeypatch.delenv("UC_GROK_TIMEOUT_S", raising=False)
    reply(svc, "send", SEND_RESULT)
    out = _call(UCBrowserCustomLLM(svc.url), text="hello")
    (send,) = svc.ops()
    assert send.path == "/sites/grok/send"
    session_id = send.body.pop("session_id")
    assert session_id.startswith("_uc-tmp-")
    assert send.body == {
        "message": "hello", "timeout_s": 300, "wait_for_response": True, "ephemeral": True}
    assert "stream" not in send.body
    # Closing the ephemeral session is the only other request: it drops the
    # service's page and map entry and never touches the account conversation.
    assert svc.deletes() == [f"/sites/grok/sessions/{session_id}"]
    assert out.choices[0].message.content == "hello back"
    assert out._hidden_params["uc_conversation_url"] == URL
    assert out._hidden_params["uc_conversation_id"] == CID


def test_provider_explicit_session_persists_and_is_not_forgotten(svc):
    reply(svc, "send", SEND_RESULT)
    provider = UCBrowserCustomLLM(svc.url)
    for _ in range(2):
        _call(provider, optional_params={"extra_body": {"session_id": "user-42"}})
    assert [c.body["session_id"] for c in svc.ops()] == ["user-42", "user-42"]
    assert all("conversation_url" not in c.body for c in svc.ops())
    assert svc.deletes() == []


def test_provider_streams_ndjson_deltas_live_then_reconciles(svc):
    gate = threading.Event()
    svc.route("POST", "/sites/grok/send", lambda m, b: (200, Ndjson(
        [{"delta": "Hel"}, {"delta": "lo"}, {"result": {**SEND_RESULT, "response": "Hello!"}}], gate,
    )))
    provider = UCBrowserCustomLLM(svc.url)
    chunks = provider.streaming(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_model_response(),
        optional_params={},
    )
    first = next(chunks)
    # The server is still holding back the rest of the reply.
    assert first["text"] == "Hel"
    assert not gate.is_set()
    gate.set()
    rest = list(chunks)
    assert [c["text"] for c in rest[:-1]] == ["lo", "!"]
    assert rest[-1]["is_finished"] is True
    assert svc.ops()[0].body["stream"] is True
    assert len(svc.deletes()) == 1


def test_provider_async_streaming_over_http(svc):
    svc.route("POST", "/sites/grok/send", lambda m, b: (200, Ndjson(
        [{"delta": "one"}, {"result": {**SEND_RESULT, "response": "one"}}],
    )))

    async def _go():
        return [c async for c in UCBrowserCustomLLM(svc.url).astreaming(
            model="grok",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_model_response(),
            optional_params={},
        )]

    chunks = asyncio.run(_go())
    assert [c["text"] for c in chunks] == ["one", ""]
    assert chunks[-1]["is_finished"] is True


def test_provider_stream_error_line_maps_to_the_litellm_error_without_resend(svc):
    svc.route("POST", "/sites/grok/send", lambda m, b: (200, Ndjson(
        [{"delta": "par"}, {"error": "auth_required", "detail": "signed out"}],
    )))
    chunks = UCBrowserCustomLLM(svc.url).streaming(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_model_response(),
        optional_params={},
    )
    assert next(chunks)["text"] == "par"
    with pytest.raises(litellm.exceptions.AuthenticationError):
        next(chunks)
    assert len(svc.ops()) == 1


@pytest.mark.parametrize(
    "status, code, expected",
    [
        (401, "auth_required", litellm.exceptions.AuthenticationError),
        (504, "timeout", litellm.exceptions.Timeout),
        (503, "busy", litellm.exceptions.ServiceUnavailableError),
        (409, "page_deviation", litellm.exceptions.ServiceUnavailableError),
        (409, "folder_mismatch", litellm.exceptions.BadRequestError),
        (404, "no_such_site", litellm.exceptions.ServiceUnavailableError),
    ],
)
def test_provider_http_errors_map_to_litellm_errors_and_never_resend(svc, status, code, expected):
    fail(svc, "send", status, code, "partial secret text")
    with pytest.raises(expected) as caught:
        _call(UCBrowserCustomLLM(svc.url))
    if code == "timeout":
        # Partial text is account content: exceptions get logged, so it stays out.
        assert "partial secret text" not in str(caught.value)
    assert len(svc.ops()) == 1
    assert len(svc.deletes()) == 1  # the ephemeral session is still closed


def test_provider_with_an_unreachable_service_is_service_unavailable():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(litellm.exceptions.ServiceUnavailableError, match="unreachable"):
        _call(UCBrowserCustomLLM(f"http://127.0.0.1:{port}"))


def test_provider_delete_after_capture_and_cleanup_use_the_site_routes(svc, monkeypatch):
    monkeypatch.setenv("UC_GROK_DELETE_AFTER_CAPTURE", "1")
    reply(svc, "send", SEND_RESULT)
    reply(svc, "delete", True)
    reply(svc, "cleanup_threads", {"summary": "kept 1, deleted 2", "deleted_ids": ["x", "y"]})
    provider = UCBrowserCustomLLM(svc.url)
    _call(provider, optional_params={"session_id": "s1"})
    send, delete = svc.ops()
    assert delete.path == "/sites/grok/delete"
    assert delete.body == {"conversation_url": URL, "session_id": "s1"}
    # The deleted conversation's session is dropped, so it starts fresh.
    assert svc.deletes() == ["/sites/grok/sessions/s1"]

    out = _call(provider, optional_params={"action": "cleanup_threads", "keep_recent": 1})
    cleanup = svc.ops("cleanup_threads")[0]
    assert cleanup.body == {"keep_recent": 1, "session_id": "_uc-admin"}
    assert out.choices[0].message.content == "kept 1, deleted 2"
    # No client-side list/delete loop: one request, the service decides.
    assert [c.path for c in svc.ops()] == [
        "/sites/grok/send", "/sites/grok/delete", "/sites/grok/cleanup_threads",
    ]


def test_provider_without_an_address_names_the_setting_and_never_calls_anything(svc):
    with pytest.raises(litellm.exceptions.ServiceUnavailableError, match="UC_GROK_SERVICE_URL"):
        _call(UCBrowserCustomLLM())
    assert svc.calls == []


@pytest.mark.parametrize("configured", [True, False])
def test_module_handler_reads_the_consumer_environment_explicitly(configured):
    """``uc_handler`` is built once at import from ``UC_GROK_SERVICE_URL``;
    unset means no client at all, never a default address."""
    env = {k: v for k, v in os.environ.items() if k != "UC_GROK_SERVICE_URL"}
    env["PYTHONPATH"] = str(REPO)
    if configured:
        env["UC_GROK_SERVICE_URL"] = "http://127.0.0.1:4010"
    code = (
        "from uc_browser.llm_providers.uc import uc_handler\n"
        "print(uc_handler._client.url if uc_handler._client else None)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, env=env,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip().splitlines()[-1] == ("http://127.0.0.1:4010" if configured else "None")


def test_grok_consumers_run_without_loading_any_browser_module():
    """Import AND exercise both consumers in a fresh interpreter (a provider
    request with no service configured, MCP registration and a tool call),
    then look at what actually got loaded."""
    code = (
        "import sys\n"
        "import litellm\n"
        "from uc_browser.llm_providers.uc import UCBrowserCustomLLM\n"
        "from uc_browser.mcp.grok_tools import register_grok_tools\n"
        "\n"
        "class Mcp:\n"
        "    tools = {}\n"
        "    def tool(self):\n"
        "        return lambda fn: self.tools.setdefault(fn.__name__, fn)\n"
        "    def prompt(self, **meta):\n"
        "        return lambda fn: fn\n"
        "\n"
        "mcp = Mcp()\n"
        "register_grok_tools(mcp)\n"
        "assert mcp.tools['grok_get_models']()['error'] == 'service_not_configured'\n"
        "try:\n"
        "    UCBrowserCustomLLM().completion(\n"
        "        model='grok', messages=[{'role': 'user', 'content': 'hi'}],\n"
        "        model_response=litellm.ModelResponse(choices=[{'index': 0, 'message': "
        "{'role': 'assistant', 'content': ''}, 'finish_reason': None}]),\n"
        "        optional_params={})\n"
        "except litellm.exceptions.ServiceUnavailableError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('an unconfigured provider answered')\n"
        "banned = ('playwright', 'uc_browser.browser', 'uc_browser.sites.grok',\n"
        "          'uc_browser.sites.grok_fast', 'uc_browser.service.targets')\n"
        "bad = [m for m in sys.modules if m in banned or m.startswith('playwright.')]\n"
        "print(bad)\n"
        "sys.exit(1 if bad else 0)\n"
    )
    env = {k: v for k, v in os.environ.items()
           if k not in ("UC_GROK_SERVICE_URL", "UC_BACKUP_MODEL")}
    env["UC_AVAILABILITY_GATE"] = "0"
    env["PYTHONPATH"] = str(REPO)
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, env=env,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_ephemeral_flag_is_sent_only_when_true(svc):
    from uc_browser.service.client import ServiceClient

    reply(svc, "send", SEND_RESULT)
    client = ServiceClient(svc.url)
    client.site("grok", "send", {"message": "a"}, session_id="s")
    client.site("grok", "send", {"message": "b"}, session_id="s", ephemeral=False)
    client.site("grok", "send", {"message": "c"}, session_id="s", ephemeral=True)
    first, second, third = (c.body for c in svc.ops())
    assert "ephemeral" not in first and "ephemeral" not in second
    assert third["ephemeral"] is True
    # It marks the session only: the client closes nothing by itself.
    assert svc.deletes() == []


def test_a_blind_ephemeral_send_is_marked_and_its_page_is_left_for_idle_expiry(svc):
    reply(svc, "send", {"response": "", "url": URL, "conversation_id": CID})
    _call(UCBrowserCustomLLM(svc.url), optional_params={"wait_for_response": False})
    (send,) = svc.ops()
    assert send.body["ephemeral"] is True and send.body["wait_for_response"] is False
    assert svc.deletes() == []


def test_follow_on_operations_on_an_ephemeral_session_keep_its_marking(svc, monkeypatch):
    monkeypatch.setenv("UC_GROK_DELETE_AFTER_CAPTURE", "1")
    reply(svc, "send", SEND_RESULT)
    reply(svc, "delete", True)
    _call(UCBrowserCustomLLM(svc.url))
    send, delete = svc.ops()
    assert send.body["session_id"] == delete.body["session_id"]
    assert send.body["ephemeral"] is True and delete.body["ephemeral"] is True
    assert delete.body["conversation_url"] == URL
    assert svc.deletes() == [f"/sites/grok/sessions/{send.body['session_id']}"]


# ── MCP tools over HTTP ──────────────────────────────────────────────────


class FakeMcp:
    """Just enough of FastMCP: records what ``register_grok_tools`` attaches."""

    def __init__(self):
        self.tools: dict = {}
        self.prompts: dict = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco

    def prompt(self, name=None, **meta):
        def deco(fn):
            self.prompts[name or fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def tools(svc):
    mcp = FakeMcp()
    register_grok_tools(mcp, service_url=svc.url)
    return mcp.tools


def test_the_public_tool_names_and_signatures_are_unchanged(svc):
    mcp = FakeMcp()
    register_grok_tools(mcp, service_url=svc.url)
    assert set(mcp.tools) == {
        "chat", "grok_send_message", "grok_read_conversation", "grok_list_conversations",
        "grok_delete_conversation", "grok_new_chat", "grok_rename_conversation",
        "grok_archive_conversation", "grok_regenerate", "grok_stop_generation",
        "grok_get_models", "grok_switch_model", "list_chat_sessions", "forget_chat_session",
    }
    assert set(mcp.prompts) == {"talk_to_grok"}

    def sig(name):
        return {
            p.name: (None if p.default is inspect.Parameter.empty else p.default)
            for p in inspect.signature(mcp.tools[name]).parameters.values()
        }

    assert sig("chat") == {
        "message": None, "session_id": "default", "wait_for_response": True, "timeout_s": 60}
    assert sig("grok_send_message") == {
        "message": None, "conversation_url": None, "timeout_s": 60}
    assert sig("grok_regenerate") == {"conversation_url": None, "timeout_s": 60}
    assert sig("grok_rename_conversation") == {"conversation_url": None, "new_title": None}
    assert sig("grok_list_conversations") == {}
    assert sig("forget_chat_session") == {"session_id": None}


def test_chat_continues_the_service_session_named_by_session_id(svc, tools):
    reply(svc, "send", SEND_RESULT)
    out = tools["chat"]("hello", session_id="abc", wait_for_response=False, timeout_s=9)
    assert out == {"ok": True, "session_id": "abc", **SEND_RESULT}
    (send,) = svc.ops()
    assert send.body == {
        "message": "hello", "timeout_s": 9, "wait_for_response": False, "session_id": "abc"}
    assert svc.deletes() == []


def test_chat_default_session_is_the_service_default_chat(svc, tools):
    reply(svc, "send", SEND_RESULT)
    assert tools["chat"]("hello")["session_id"] == "default"
    assert svc.ops()[0].body["session_id"] == "default"


def test_send_message_never_continues_a_chat_session(svc, tools):
    reply(svc, "send", SEND_RESULT)
    out = tools["grok_send_message"]("hello")
    assert out == {"ok": True, **SEND_RESULT}
    (send,) = svc.ops()
    session_id = send.body.pop("session_id")
    assert session_id.startswith("_uc-tmp-") and session_id != "default"
    # No URL: a new conversation, in a session the service must not persist.
    assert send.body == {"message": "hello", "timeout_s": 60, "ephemeral": True}
    assert svc.deletes() == [f"/sites/grok/sessions/{session_id}"]


def test_send_message_passes_an_explicit_url_as_provided_and_still_forgets_the_session(svc, tools):
    reply(svc, "send", SEND_RESULT)
    tools["grok_send_message"]("hello", conversation_url="https://grok.com/c/xyz", timeout_s=5)
    (send,) = svc.ops()
    assert send.body["conversation_url"] == "https://grok.com/c/xyz"
    assert send.body["timeout_s"] == 5
    assert len(svc.deletes()) == 1


def test_send_message_failure_still_closes_its_session_and_reports_the_error(svc, tools):
    fail(svc, "send", 409, "folder_mismatch", "outside the folder")
    out = tools["grok_send_message"]("hello", conversation_url="https://grok.com/c/other")
    assert out == {"ok": False, "error": "folder_mismatch", "message": "outside the folder"}
    assert len(svc.deletes()) == 1


def test_admin_tools_make_one_request_each_and_keep_their_result_shapes(svc, tools):
    reply(svc, "read", {"url": URL, "conversation_id": CID, "messages": [{"role": "user", "text": "x"}]})
    reply(svc, "list_conversations", [{"id": CID, "title": "t", "url": URL}])
    reply(svc, "delete", True)
    reply(svc, "new_chat", {"url": "https://grok.com/project/p"})
    reply(svc, "rename", True)
    reply(svc, "archive", False)
    reply(svc, "regenerate", {"ok": True, "response": "again"})
    reply(svc, "get_models", ["a", "b"])
    reply(svc, "switch_model", True)

    assert tools["grok_read_conversation"](URL)["messages"][0]["text"] == "x"
    assert tools["grok_list_conversations"]() == {
        "ok": True, "conversations": [{"id": CID, "title": "t", "url": URL}], "count": 1}
    assert tools["grok_delete_conversation"](URL) == {"ok": True, "deleted": True}
    assert tools["grok_new_chat"]() == {"ok": True, "url": "https://grok.com/project/p"}
    assert tools["grok_rename_conversation"](URL, "new") == {"ok": True, "renamed": True}
    assert tools["grok_archive_conversation"](URL) == {"ok": True, "archived": False}
    assert tools["grok_regenerate"](URL, timeout_s=7) == {"ok": True, "response": "again"}
    assert tools["grok_get_models"]() == {"ok": True, "models": ["a", "b"]}
    assert tools["grok_switch_model"]("m") == {"ok": True, "switched": True}

    bodies = {c.path.rsplit("/", 1)[1]: c.body for c in svc.ops()}
    admin = {"session_id": "_uc-admin"}
    assert bodies["read"] == {"conversation_url": URL, **admin}
    assert bodies["list_conversations"] == admin
    assert bodies["delete"] == {"conversation_url": URL, **admin}
    assert bodies["rename"] == {"conversation_url": URL, "new_title": "new", **admin}
    assert bodies["regenerate"] == {"conversation_url": URL, "timeout_s": 7, **admin}
    assert bodies["switch_model"] == {"model_name": "m", **admin}
    assert len(svc.ops()) == 9
    assert svc.deletes() == []


def test_stop_names_the_conversation_and_no_session(svc, tools):
    reply(svc, "stop", True)
    assert tools["grok_stop_generation"](URL) == {"ok": True, "stopped": True}
    (stop,) = svc.ops()
    # The service finds the busy page by URL; naming a session would aim the
    # stop at the wrong (idle) one.
    assert stop.body == {"conversation_url": URL}


def test_list_chat_sessions_shows_only_conversation_backed_user_sessions(svc, tools):
    svc.route("GET", "/sites/grok/sessions", lambda m, b: (200, {
        "default": {"url": URL, "live": True, "idle_s": 1.0},
        "fresh": {"url": None, "live": True, "idle_s": 0.0},
        "_uc-admin": {"url": "https://grok.com/project/p", "live": True, "idle_s": 2.0},
        "_uc-tmp-1": {"url": URL, "live": True, "idle_s": 0.0},
    }))
    assert tools["list_chat_sessions"]() == {"ok": True, "sessions": {"default": URL}, "count": 1}


def test_forget_chat_session_reports_what_it_dropped(svc, tools):
    svc.route("GET", "/sites/grok/sessions", lambda m, b: (200, {
        "abc": {"url": URL, "live": False, "idle_s": 5.0},
    }))
    assert tools["forget_chat_session"]("abc") == {"ok": True, "forgotten": True, "previous_url": URL}
    assert svc.deletes() == ["/sites/grok/sessions/abc"]


def test_forget_chat_session_for_an_unknown_id_is_not_an_error(svc, tools):
    svc.route("GET", "/sites/grok/sessions", lambda m, b: (200, {}))
    svc.routes.insert(0, ("DELETE", re.compile("/sites/grok/sessions/nope"),
                          lambda m, b: (404, {"error": "no_such_session", "detail": "nope"})))
    assert tools["forget_chat_session"]("nope") == {
        "ok": True, "forgotten": False, "previous_url": None}


def test_reserved_session_ids_are_refused(svc, tools):
    for out in (tools["chat"]("hi", session_id="_uc-admin"), tools["forget_chat_session"]("_uc-admin")):
        assert out["ok"] is False
    assert svc.calls == []


@pytest.mark.parametrize(
    "status, code",
    [(401, "auth_required"), (503, "busy"), (409, "page_deviation"), (504, "timeout")],
)
def test_service_errors_come_back_as_error_dicts(svc, tools, status, code):
    fail(svc, "list_conversations", status, code, "why")
    assert tools["grok_list_conversations"]() == {"ok": False, "error": code, "message": "why"}


def test_an_unreachable_service_is_an_error_dict():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    mcp = FakeMcp()
    register_grok_tools(mcp, service_url=f"http://127.0.0.1:{port}")
    out = mcp.tools["grok_get_models"]()
    assert out["ok"] is False and out["error"] == "service_gone"


def test_a_result_of_the_wrong_shape_is_reported_not_guessed(svc, tools):
    reply(svc, "list_conversations", {"unexpected": "object"})
    out = tools["grok_list_conversations"]()
    assert out["ok"] is False and out["error"] == "bad_response"


def test_without_any_address_every_tool_reports_service_not_configured():
    mcp = FakeMcp()
    register_grok_tools(mcp)
    for out in (mcp.tools["chat"]("hi"), mcp.tools["grok_list_conversations"](),
                mcp.tools["list_chat_sessions"]()):
        assert out["ok"] is False and out["error"] == "service_not_configured"
        assert "UC_GROK_SERVICE_URL" in out["message"]


def test_the_consumer_environment_supplies_the_address_and_the_argument_wins(svc, monkeypatch):
    reply(svc, "get_models", ["from-env"])
    monkeypatch.setenv("UC_GROK_SERVICE_URL", svc.url)
    mcp = FakeMcp()
    register_grok_tools(mcp)
    assert mcp.tools["grok_get_models"]() == {"ok": True, "models": ["from-env"]}

    other = Service()
    try:
        other.route("POST", "/sites/grok/get_models", lambda m, b: (200, {"result": ["from-arg"]}))
        mcp2 = FakeMcp()
        register_grok_tools(mcp2, service_url=other.url)
        assert mcp2.tools["grok_get_models"]() == {"ok": True, "models": ["from-arg"]}
    finally:
        other.close()


def test_a_malformed_address_fails_at_registration():
    with pytest.raises(ValueError):
        register_grok_tools(FakeMcp(), service_url="127.0.0.1:4010")
