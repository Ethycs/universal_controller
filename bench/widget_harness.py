"""Local widget harness — embed each vendor's real loader script in a
controlled page and probe what it injects.

Why: probing vendor marketing homepages conflates "does the engine
detect a widget" with "does this vendor happen to show one on their
homepage." A harness page isolates the widget engine itself.

Honest limits (stated so results aren't over-read):
  * Placeholder credentials. Without a real per-vendor account, most
    loaders inject only their LAUNCHER chrome (the bubble), not a live
    composer. That still exercises the launcher-open recall gap the
    vendor probe found — the actual blocker — but a gate-clearing
    composer usually needs real credentials or a genuine widget iframe.
  * Loader URLs are reconstructed from the canonical vendor CDNs (the
    pasted snippets were transit-mangled); set real IDs via a creds JSON
    to get live composers (see --creds).

Usage:
  pixi run python -m bench.widget_harness                 # all, placeholder creds
  pixi run python -m bench.widget_harness --only crisp,gorgias
  pixi run python -m bench.widget_harness --creds my_creds.json --capture
"""

from __future__ import annotations

import argparse
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from uc_browser._paths import _SUBMODULE_ROOT  # noqa: E402
from uc_browser.health import HealthMonitor  # noqa: E402
from uc_browser.lab import _DISMISS_CONSENT_JS  # noqa: E402

FIXTURES = _SUBMODULE_ROOT / "bench" / "fixtures"
DETECT_GATE = 4.0

# name -> loader <script> body. {ID} substituted from creds (else placeholder).
# URLs are the canonical vendor CDNs (reconstructed; pasted ones were mangled).
LOADERS: dict[str, str] = {
    "intercom": """
window.intercomSettings={app_id:"{ID}"};
(function(){var w=window,ic=w.Intercom;if(typeof ic==="function"){ic('reattach_activator');ic('update',w.intercomSettings);}else{var d=document,i=function(){i.c(arguments);};i.q=[];i.c=function(a){i.q.push(a);};w.Intercom=i;var l=function(){var s=d.createElement('script');s.async=true;s.src='https://widget.intercom.io/widget/{ID}';var x=d.getElementsByTagName('script')[0];x.parentNode.insertBefore(s,x);};l();}})();
""",
    "zendesk": """
var s=document.createElement('script');s.id='ze-snippet';
s.src='https://static.zdassets.com/ekr/snippet.js?key={ID}';
document.head.appendChild(s);
""",
    "crisp": """
window.$crisp=[];window.CRISP_WEBSITE_ID="{ID}";
(function(){var d=document,s=d.createElement("script");s.src="https://client.crisp.chat/l.js";s.async=1;d.getElementsByTagName("head")[0].appendChild(s);})();
""",
    "ada": """
window.adaSettings={handle:"{ID}"};
var s=document.createElement('script');s.src='https://static.ada.support/embed2.js';s.id='__ada';s.setAttribute('data-handle','{ID}');s.async=true;document.head.appendChild(s);
""",
    "gorgias": """
var s=document.createElement('script');s.id='gorgias-chat-widget-install-v3';
s.src='https://config.gorgias.chat/bundle-loader/{ID}';document.head.appendChild(s);
""",
    "kustomer": """
var s=document.createElement('script');s.src='https://cdn.kustomerapp.com/chat-web/widget.js';
s.setAttribute('data-kustomer-api-key','{ID}');document.head.appendChild(s);
s.onload=function(){if(window.Kustomer)window.Kustomer.start();};
""",
    "freshchat": """
var s=document.createElement('script');s.src='https://wchat.freshchat.com/js/widget.js';s.async=true;
s.onload=function(){window.fcWidget&&window.fcWidget.init({token:"{ID}",host:"https://wchat.freshchat.com"});};
document.head.appendChild(s);
""",
    "liveperson": """
window.lpTag=window.lpTag||{};
var s=document.createElement('script');s.src='https://lptag.liveperson.net/tag/tag.js?site={ID}';s.async=true;document.head.appendChild(s);
""",
    "landbot": """
var s=document.createElement('script');s.src='https://cdn.landbot.io/landbot-3/landbot-3.0.0.js';s.async=true;
s.onload=function(){new window.Landbot.Livechat({configUrl:'https://landbot.online/v3/{ID}/index.json'});};
document.head.appendChild(s);
""",
    "botpress": """
var s=document.createElement('script');s.src='https://cdn.botpress.cloud/webchat/v2.2/inject.js';s.async=true;
s.onload=function(){window.botpress&&window.botpress.init({botId:"{ID}"});};
document.head.appendChild(s);
""",
    "olark": """
;(function(o,l,a,r,k,y){if(o.olark)return;r="script";y=l.createElement(r);r=l.getElementsByTagName(r)[0];y.async=1;y.src="//"+a;r.parentNode.insertBefore(y,r);o.olark=k=function(){o.olark._.push(arguments)};k._=[];k.ready=function(){k._.push(["ready",arguments])};})(window,document,"static.olark.com/jsclient/loader0.js");
olark.identify('{ID}');
""",
}

PLACEHOLDER = "DEMO0000PLACEHOLDER"


def _page(name: str, loader: str, cred: str) -> str:
    body = loader.replace("{ID}", cred).strip()
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{name} harness</title></head>
<body><h1>{name} widget harness</h1>
<p>Loader embedded below. Credential: {'REAL' if cred != PLACEHOLDER else 'placeholder'}.</p>
<script>{body}</script>
</body></html>"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    pages: dict[str, str] = {}

    def do_GET(self):
        name = self.path.strip("/")
        html = self.pages.get(name)
        if html is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, *a):
        pass


def probe(ctx, mon, bundle, name, url, capture) -> dict:
    rec = {"name": name, "score": 0.0, "launcher": False, "frame": "",
           "injected": {}, "captured": False, "error": None}
    page = ctx.new_page()
    try:
        page.goto(url, timeout=20000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)  # loaders inject late
        for frame in page.frames:
            try:
                frame.evaluate(_DISMISS_CONSENT_JS)
            except Exception:
                pass
        # What did the loader actually inject?
        rec["injected"] = page.evaluate("""() => ({
            iframes: document.querySelectorAll('iframe').length,
            shadowHosts: [...document.querySelectorAll('*')].filter(e=>e.shadowRoot).length,
            fixedBtns: [...document.querySelectorAll('button,[role=button],a,div')]
              .filter(e=>{const s=getComputedStyle(e);const r=e.getBoundingClientRect();
                return (s.position==='fixed'||s.position==='sticky')&&r.width>20&&r.width<200
                  &&r.bottom>window.innerHeight*0.5;}).length,
        })""")
        score, where = mon._detect_across_frames(page, bundle)
        if score < DETECT_GATE and mon._try_open_widget(page):
            rec["launcher"] = True
            page.wait_for_timeout(4000)
            score, where = mon._detect_across_frames(page, bundle)
        rec["score"] = round(float(score), 2)
        rec["frame"] = where or ""
        if score >= DETECT_GATE and capture:
            out = FIXTURES / f"harness-{name}"
            out.mkdir(parents=True, exist_ok=True)
            cdp = ctx.new_cdp_session(page)
            snap = cdp.send("Page.captureSnapshot", {"format": "mhtml"})
            (out / "page.mhtml").write_text(snap["data"], encoding="utf-8",
                                            newline="")
            (out / "meta.json").write_text(json.dumps({
                "name": f"harness-{name}", "url": url, "kind": "widget",
                "vendor": name, "truth": {}, "resolved": {},
                "provenance": "widget-harness",
            }, indent=2), encoding="utf-8")
            rec["captured"] = True
        return rec
    except Exception as e:
        rec["error"] = str(e)[:80]
        return rec
    finally:
        page.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="comma-separated vendor names")
    ap.add_argument("--creds", help="JSON {vendor: real_id} for live composers")
    ap.add_argument("--capture", action="store_true",
                    help="save fixtures for gate-clearing hits")
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace",
                               line_buffering=True)
    except Exception:
        pass

    creds = {}
    if args.creds:
        creds = json.loads(Path(args.creds).read_text(encoding="utf-8"))

    names = list(LOADERS)
    if args.only:
        want = {v.strip() for v in args.only.split(",")}
        names = [n for n in names if n in want]

    _Handler.pages = {
        n: _page(n, LOADERS[n], creds.get(n, PLACEHOLDER)) for n in names
    }
    httpd = socketserver.TCPServer(("127.0.0.1", args.port), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    mon = HealthMonitor()
    bundle = mon._bundle_js()
    if not bundle:
        print("UC bundle not built.")
        return 1

    from playwright.sync_api import sync_playwright
    profile_dir = _SUBMODULE_ROOT / "data" / ".lab_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir), headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-32000,-32000"],
            viewport={"width": 1440, "height": 900})
        for n in names:
            has_cred = n in creds
            print(f"harness {n} ({'REAL cred' if has_cred else 'placeholder'}) ...",
                  flush=True)
            results.append(probe(ctx, mon, bundle, n,
                                 f"http://127.0.0.1:{args.port}/{n}",
                                 capture=args.capture))
        ctx.close()
    httpd.shutdown()

    print(f"\n{'vendor':12s} {'score':>5s} {'inject(if/sh/fx)':16s} {'launcher':8s} detail")
    print("-" * 72)
    inj_any = hits = 0
    for r in sorted(results, key=lambda x: -x["score"]):
        i = r["injected"]
        inj = f"{i.get('iframes',0)}/{i.get('shadowHosts',0)}/{i.get('fixedBtns',0)}" if i else "-"
        injected = bool(i and (i.get("iframes") or i.get("shadowHosts") or i.get("fixedBtns")))
        inj_any += injected
        hit = r["score"] >= DETECT_GATE
        hits += hit
        detail = r["error"] or ("composer" if hit else
                                ("chrome injected" if injected else "nothing injected"))
        print(f"{r['name']:12s} {r['score']:5.1f} {inj:16s} "
              f"{'clicked' if r['launcher'] else '-':8s} {detail[:30]}")
    print("-" * 72)
    print(f"{hits}/{len(results)} composer-detected · {inj_any}/{len(results)} "
          "injected launcher chrome (placeholder creds cap most at chrome).")
    print("For live composers: --creds creds.json with real per-vendor IDs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
