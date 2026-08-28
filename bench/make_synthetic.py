"""Generate synthetic MHTML fixtures — always-available smoke tests for
the bench runner itself (no network, no capture flakiness).

  synthetic-chat      minimal well-formed chat UI (log + composer + send)
  synthetic-search    search-box page that must NOT read as a chat

Usage: pixi run python bench/make_synthetic.py
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench._common import FIXTURES_DIR, write_meta  # noqa: E402

CHAT_HTML = """<!doctype html>
<html><head><title>Synthetic Chat</title><style>
  body { margin: 0; font-family: sans-serif; }
  .chat-log { height: 400px; overflow-y: auto; border: 1px solid #ccc; }
  .msg { padding: 8px; margin: 4px; background: #eee; border-radius: 8px; }
  .composer { position: fixed; bottom: 0; width: 100%; display: flex; }
  .composer [contenteditable] { flex: 1; border: 1px solid #999; min-height: 40px; padding: 8px; }
</style></head>
<body>
  <div class="chat-log" role="log" aria-live="polite" data-uc-truth="messages">
    <div class="msg">Hello, how can I help?</div>
    <div class="msg">Tell me about MHTML.</div>
    <div class="msg">MHTML is a web archive format.</div>
  </div>
  <div class="composer">
    <div contenteditable="true" role="textbox" aria-label="Message input"
         data-uc-truth="input"></div>
    <button aria-label="Send message" data-uc-truth="send">Send</button>
  </div>
</body></html>"""

SEARCH_HTML = """<!doctype html>
<html><head><title>Synthetic Search</title><style>
  body { font-family: sans-serif; text-align: center; padding-top: 120px; }
  input { width: 400px; padding: 10px; }
</style></head>
<body>
  <h1>FindStuff</h1>
  <form role="search" action="/search">
    <input type="text" name="q" placeholder="Search the web" aria-label="Search">
    <button type="submit">Search</button>
  </form>
  <p><a href="/about">About</a> · <a href="/images">Images</a></p>
</body></html>"""

FIXTURES = [
    ("synthetic-chat", "chat", CHAT_HTML,
     {"input": ['[data-uc-truth="input"]'],
      "send": ['[data-uc-truth="send"]'],
      "messages": ['[data-uc-truth="messages"]']}),
    ("synthetic-search", "negative", SEARCH_HTML, {}),
]


def main() -> int:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        for name, kind, html, truth in FIXTURES:
            page = ctx.new_page()
            page.set_content(html, wait_until="load")
            cdp = ctx.new_cdp_session(page)
            snap = cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            out_dir = FIXTURES_DIR / name
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "page.mhtml").write_text(
                snap["data"], encoding="utf-8", newline="")
            write_meta(out_dir, {
                "name": name, "url": "synthetic://" + name, "kind": kind,
                "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "truth": truth, "resolved": {}, "synthetic": True,
            })
            page.close()
            print(f"wrote {out_dir}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
