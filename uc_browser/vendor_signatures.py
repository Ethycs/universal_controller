"""Vendor signatures — identify the chat *engine* on a page by its loader
fingerprint, then apply that engine's known launcher/composer recipe.

This is the high-leverage tier of the Signature Lab: a vendor signature
is NOT site-specific — one entry covers every site running that engine
(thousands of them). It sits between the pure generic heuristic and
per-site profiles: when the structural detector can't find a composer
(the launcher-open recall gap), we ask "which engine is this?" by
scanning script srcs + window globals, and if we recognize it, drive its
known launcher.

A signature carries:
  fingerprints : substrings that, if present in any <script src> or as a
                 window global, identify the engine
  launcher     : CSS selectors for the open-chat control (tried in order,
                 searched across all frames)
  open_global  : optional JS to call the vendor API's open method directly
                 (more reliable than clicking when available)

Design fit: this is vendor knowledge, not site knowledge — it generalizes
by construction, so it belongs in the engine layer, not in bench oracles.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VendorSignature:
    name: str
    fingerprints: list[str]              # script-src substrings
    globals: list[str] = field(default_factory=list)   # window.<name>
    launcher: list[str] = field(default_factory=list)  # CSS, tried in order
    open_global: str = ""                # JS expression that opens the widget


# Loader CDNs + window globals + launcher selectors for the major engines.
# Sources: the vendors' documented install snippets + widget DOM.
SIGNATURES: list[VendorSignature] = [
    VendorSignature(
        "intercom", ["widget.intercom.io", "js.intercomcdn.com", "intercom.io/widget"],
        ["Intercom"],
        [".intercom-launcher", '[aria-label*="Open Intercom" i]',
         ".intercom-launcher-frame", "#intercom-container .intercom-launcher"],
        "window.Intercom && window.Intercom('show')"),
    VendorSignature(
        "zendesk", ["static.zdassets.com", "ekr/snippet.js", "zopim"],
        ["zE", "$zopim"],
        ["#launcher", 'iframe#launcher', '[data-testid="launcher"]',
         'button[aria-label*="chat" i]'],
        "window.zE && window.zE('messenger', 'open')"),
    VendorSignature(
        "crisp", ["client.crisp.chat"], ["$crisp"],
        ['.crisp-client [aria-label*="chat" i]', "#crisp-chatbox a",
         '[data-id="chat"]', ".cc-1brb6"],
        "window.$crisp && window.$crisp.push(['do','chat:open'])"),
    VendorSignature(
        "ada", ["static.ada.support"], ["adaEmbed", "adaSettings"],
        ["#ada-button-frame", '[id*="ada-button"]', 'iframe[title*="Ada" i]'],
        "window.adaEmbed && window.adaEmbed.toggle()"),
    VendorSignature(
        "gorgias", ["config.gorgias.chat", "gorgias.chat"], ["GorgiasChat"],
        ["#gorgias-chat-container button", '[class*="gorgias-chat"]',
         "#chat-button"],
        "window.GorgiasChat && window.GorgiasChat.open()"),
    VendorSignature(
        "kustomer", ["cdn.kustomerapp.com", "kustomerapp.com"], ["Kustomer"],
        ["#kustomer-ui-sdk-launcher-icon-frame",
         '[id*="kustomer"][id*="launcher"]'],
        "window.Kustomer && window.Kustomer.open()"),
    VendorSignature(
        "freshchat", ["wchat.freshchat.com", "freshchat.com/js"], ["fcWidget"],
        ["#fc_frame", "#freshchat-frame", '[id*="fc_"] button'],
        "window.fcWidget && window.fcWidget.open()"),
    VendorSignature(
        "liveperson", ["lptag.liveperson.net", "lpsnmedia.net"], ["lpTag"],
        [".lp-window-root button", '[data-lp-event]',
         'button[aria-label*="chat" i]'],
        ""),
    VendorSignature(
        "landbot", ["cdn.landbot.io", "landbot.online"], ["Landbot"],
        ["#landbot-widget button", ".LandbotLivechat", ".lb-livechat-launcher"],
        ""),
    VendorSignature(
        "botpress", ["cdn.botpress.cloud", "botpress.com/webchat"],
        ["botpress", "botpressWebChat"],
        [".bpFab", "#bp-widget-web button", "#webchat button"],
        "window.botpress && window.botpress.open()"),
    VendorSignature(
        "olark", ["static.olark.com"], ["olark"],
        ["#olark-box", ".olark-launch-button", "#habla_beta_container_do_not_rely_on_div_id_or_class button"],
        "window.olark && window.olark('api.box.expand')"),
    VendorSignature(
        "drift", ["js.driftt.com", "driftt.com", "drift.com/include"],
        ["drift", "driftt"],
        [".drift-open-chat", "#drift-widget", 'iframe[id*="drift"]',
         'button[aria-label*="chat" i]'],
        "window.drift && window.drift.api && window.drift.api.openChat()"),
    VendorSignature(
        "tawk", ["embed.tawk.to"], ["Tawk_API"],
        ["#tawkchat-minified", ".tawk-button", 'iframe[title*="chat" i]'],
        "window.Tawk_API && window.Tawk_API.maximize()"),
    VendorSignature(
        "tidio", ["code.tidio.co"], ["tidioChatApi"],
        ["#tidio-chat-iframe", "#tidio-chat button"],
        "window.tidioChatApi && window.tidioChatApi.open()"),
    VendorSignature(
        "zoho-salesiq", ["salesiq.zoho", "salesiq.zohopublic"], ["$zoho"],
        [".zsiq_float", "#zsiq_agtpic", ".siqico-chat"],
        "window.$zoho && $zoho.salesiq && $zoho.salesiq.floatwindow.visible('show')"),
    VendorSignature(
        "livechat", ["cdn.livechatinc.com", "cdn.livechat-static.com"],
        ["LiveChatWidget", "LC_API"],
        ["#chat-widget-container button", '[aria-label*="open chat" i]'],
        "window.LiveChatWidget && window.LiveChatWidget.call('maximize')"),
    VendorSignature(
        # Chat runtime only — js.hs-scripts.com is HubSpot *tracking*,
        # present on countless non-chat sites (false-positive source).
        "hubspot", ["js.usemessages.com", "js.hs-banner.com/conversations"],
        ["HubSpotConversations"],
        ["#hubspot-messages-iframe-container button",
         "#hubspot-messages-iframe-container iframe"],
        "window.HubSpotConversations && window.HubSpotConversations.widget.open()"),
]


# ── in-page identifier ────────────────────────────────────────────────
# Returns the list of vendor names whose fingerprint is present. Scans
# script srcs and probes window globals. Runs per-frame.
IDENTIFY_JS = r"""(sigs) => {
  const srcs = [...document.querySelectorAll('script[src]')]
    .map(s => s.src.toLowerCase());
  const inlineHtml = document.documentElement.outerHTML.toLowerCase().slice(0, 200000);
  const hits = [];
  for (const sig of sigs) {
    let found = false;
    for (const fp of sig.fingerprints) {
      const f = fp.toLowerCase();
      if (srcs.some(s => s.includes(f)) || inlineHtml.includes(f)) { found = true; break; }
    }
    if (!found) {
      for (const g of (sig.globals || [])) {
        try { if (typeof window[g] !== 'undefined') { found = true; break; } } catch (e) {}
      }
    }
    if (found) hits.push(sig.name);
  }
  return hits;
}"""


def _sig_payload() -> list[dict]:
    return [{"name": s.name, "fingerprints": s.fingerprints,
             "globals": s.globals} for s in SIGNATURES]


def get(name: str) -> VendorSignature | None:
    for s in SIGNATURES:
        if s.name == name:
            return s
    return None


def identify(page) -> list[str]:
    """Vendor engines detected on ``page`` (deduped across all frames),
    most-confident first (main frame before subframes).

    Prefers the in-page ``__UC_scanVendors`` scanner (resource-timing
    based — catches lazily-loaded widget loaders a static DOM read
    misses). Falls back to a static script/global scan if the bundle
    scanner isn't present (older bundle, or bundle not injected here)."""
    seen: list[str] = []
    payload = _sig_payload()
    for frame in page.frames:
        names = None
        try:
            names = frame.evaluate(
                "() => (typeof window.__UC_scanVendors === 'function') "
                "? window.__UC_scanVendors() : null")
        except Exception:
            names = None
        if names is None:
            try:
                names = frame.evaluate(IDENTIFY_JS, payload)
            except Exception:
                names = []
        for name in names or []:
            if name not in seen:
                seen.append(name)
    return seen


def open_widget(page, vendor: str) -> bool:
    """Open a known vendor's widget: try its JS open API first (most
    reliable), then click its launcher selectors across all frames.
    Returns True if an open action was dispatched."""
    sig = get(vendor)
    if not sig:
        return False
    # JS API first: confirm a vendor global is present (the open method
    # usually returns undefined, so we gate on the global existing, then
    # fire the call for its side effect).
    if sig.open_global and sig.globals:
        for frame in page.frames:
            try:
                has = frame.evaluate(
                    "(gs) => gs.some(g => { try { return typeof window[g] "
                    "!== 'undefined'; } catch (e) { return false; } })",
                    sig.globals)
                if has:
                    frame.evaluate(f"() => {{ try {{ {sig.open_global}; }} "
                                   f"catch (e) {{}} }}")
                    return True
            except Exception:
                continue
    for sel in sig.launcher:
        for frame in page.frames:
            try:
                loc = frame.locator(sel).first
                if loc.count() > 0:
                    loc.click(timeout=3000)
                    return True
            except Exception:
                continue
    return False
