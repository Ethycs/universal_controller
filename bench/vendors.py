"""Chat-widget vendor sample pages — the framework-diversity axis of the
zoo. Each vendor's own site dogfoods its own widget, so the vendor page
is a live instance of that engine's DOM shape. Capturing one fixture per
vendor covers the shapes that recur across thousands of downstream sites,
which is far higher yield than more brand homepages.

Probe/capture with:  pixi run python -m bench.capture_vendors
The verified subset becomes bench fixtures (vendor-<name>/).
"""

from __future__ import annotations

# (name, url, launcher_hint) — launcher_hint is advisory; the generic
# launcher-open heuristic runs regardless.
VENDORS = [
    # Marketing sites that embed their own widget (dogfooding).
    ("intercom", "https://www.intercom.com", "Open Intercom"),
    ("drift", "https://www.drift.com", "chat"),
    ("tidio", "https://www.tidio.com", "chat"),
    ("crisp", "https://crisp.chat/en/livechat/", "Open chat"),
    ("tawk", "https://www.tawk.to", "chat"),
    ("livechat", "https://www.livechat.com", "Open chat"),
    ("olark", "https://www.olark.com", "chat"),
    ("freshchat", "https://www.freshworks.com/live-chat-software/", "chat"),
    ("liveperson", "https://www.liveperson.com", "chat"),
    ("kustomer", "https://www.kustomer.com", "chat"),
    ("gorgias", "https://www.gorgias.com", "chat"),
    ("landbot", "https://landbot.io", "chat"),
    ("chatling", "https://chatling.ai", "chat"),
    ("zoho-salesiq", "https://www.zoho.com/salesiq/", "chat"),
    ("hubspot", "https://www.hubspot.com", "chat"),
    ("zendesk", "https://www.zendesk.com/service/messaging/live-chat-software/", "chat"),
    # AI-agent vendors (Ada, Ultimate, etc.) — modern shadow-DOM widgets.
    ("ada", "https://www.ada.cx", "chat"),
    ("intercom-fin", "https://www.intercom.com/fin", "chat"),
    # Demo/playground pages where the widget is the whole point.
    ("tidio-demo", "https://www.tidio.com/lyro-ai-chatbot/", "chat"),
    ("botpress", "https://botpress.com", "chat"),
]
