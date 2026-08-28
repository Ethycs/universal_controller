"""Benchmark site registry.

Each Site describes one page the generic engine should (or should not)
detect a chat on, plus *oracle* selectors that locate the ground-truth
elements at capture time.

IMPORTANT DESIGN RULE: site-specific selectors are allowed HERE and only
here — they are the measuring stick, not the product. Nothing in src/ or
uc_browser/ may ever reference them. The engine is scored on finding the
same elements the oracles point at, without knowing the oracles.

Oracle fields are candidate lists tried in order; the first that resolves
wins and is stamped onto the element as ``data-uc-truth="<role>"`` before
the MHTML snapshot, so fixtures are self-annotating. If none resolves,
capture still succeeds but records the miss in meta.json — refine the
oracle from the probe dump and recapture.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Site:
    name: str
    url: str
    kind: str  # "chat" | "widget" | "negative"
    truth_input: list[str] = field(default_factory=list)
    truth_send: list[str] = field(default_factory=list)
    truth_messages: list[str] = field(default_factory=list)
    # Clicks to perform after load, before capture (e.g. open a widget
    # launcher). Best-effort, searched across all frames.
    pre_clicks: list[str] = field(default_factory=list)
    wait_ms: int = 6000
    login_required: bool = False
    notes: str = ""


SITES: list[Site] = [
    # ── Chat sites (positive: engine must find input / send / stream) ──
    Site(
        name="chatgpt",
        url="https://chatgpt.com",
        kind="chat",
        truth_input=[
            "#prompt-textarea",
            "#mobile-composer-prompt",
            'textarea[placeholder*="Ask ChatGPT" i]',
            'div.ProseMirror[contenteditable="true"]',
        ],
        truth_send=[
            'button[data-testid="send-button"]',
            "#composer-submit-button",
            'button[aria-label*="Send" i]',
        ],
        truth_messages=['[data-testid^="conversation-turn"]'],
        notes="Usable logged out; Cloudflare — capture headful.",
    ),
    Site(
        name="copilot",
        url="https://copilot.microsoft.com",
        kind="chat",
        truth_input=[
            "textarea#userInput",
            'textarea[placeholder]',
            '[contenteditable="true"][role="textbox"]',
        ],
        truth_send=[
            'button[aria-label*="Submit" i]',
            'button[aria-label*="Send" i]',
            'button[title*="Submit" i]',
        ],
        login_required=True,
        notes="Logged out shows a sign-in wall (verified 2026-08).",
    ),
    Site(
        name="pi",
        url="https://pi.ai/talk",
        kind="chat",
        truth_input=[
            'textarea[placeholder*="Talk" i]',
            'input[placeholder="Preferred name"]',
            "main textarea",
        ],
        truth_send=[
            'button[aria-label*="Submit" i]',
            'main form button',
            'button[type="submit"]',
        ],
        notes="Logged out shows onboarding, which IS a chat: Pi asks your "
              "name, the composer input answers it. Valid truth.",
    ),
    Site(
        name="huggingchat",
        url="https://huggingface.co/chat/",
        kind="chat",
        truth_input=[
            'textarea[placeholder*="Ask" i]',
            'textarea[enterkeyhint="send"]',
            "textarea",
        ],
        truth_send=['button[aria-label*="Send" i]', 'button[type="submit"]'],
        login_required=True,
        notes="As of 2026-08 the welcome modal's 'Start chatting' redirects "
              "to HF OAuth — chatting needs login. Logged out, the composer "
              "sits (correctly) aria-hidden behind the modal.",
    ),
    Site(
        name="perplexity",
        url="https://www.perplexity.ai",
        kind="chat",
        truth_input=[
            '[contenteditable="true"][role="textbox"]',
            'textarea[placeholder*="Ask" i]',
            "div.ProseMirror",
        ],
        truth_send=[
            'button[data-testid="submit-button"]',
            'button[aria-label*="Submit" i]',
        ],
        notes="Cloudflare — capture headful.",
    ),
    Site(
        name="grok",
        url="https://grok.com",
        kind="chat",
        truth_input=['div.ProseMirror[contenteditable="true"]', "textarea"],
        truth_send=[
            'button[data-testid="chat-submit"]',
            'button[type="submit"]',
            'button[aria-label*="Submit" i]',
        ],
        notes="May show login wall; grok_fast.py knows these selectors too — "
              "that duplication is deliberate (oracle vs product).",
    ),
    Site(
        name="lmarena",
        url="https://lmarena.ai",
        kind="chat",
        truth_input=["textarea"],
        truth_send=['button[type="submit"]', 'button[aria-label*="Send" i]'],
    ),
    Site(
        name="claude",
        url="https://claude.ai",
        kind="chat",
        truth_input=['div.ProseMirror[contenteditable="true"]', '[contenteditable="true"]'],
        truth_send=['button[aria-label*="Send" i]'],
        login_required=True,
    ),
    Site(
        name="gemini",
        url="https://gemini.google.com/app",
        kind="chat",
        truth_input=['div[contenteditable="true"][role="textbox"]', "rich-textarea div"],
        truth_send=['button[aria-label*="Send" i]'],
        login_required=True,
    ),
    Site(
        name="deepseek",
        url="https://chat.deepseek.com",
        kind="chat",
        truth_input=["textarea#chat-input", "textarea"],
        truth_send=['div[role="button"][aria-disabled]', 'button[type="submit"]'],
        login_required=True,
    ),
    Site(
        name="poe",
        url="https://poe.com",
        kind="chat",
        truth_input=['textarea[class*="GrowingTextArea"]', "textarea"],
        truth_send=['button[data-button-send="true"]', 'button[aria-label*="Send" i]'],
        login_required=True,
    ),

    # ── Widget platforms (positive, usually inside iframes) ────────────
    Site(
        name="tidio",
        url="https://www.tidio.com",
        kind="widget",
        pre_clicks=['iframe[title*="Tidio" i]', "#tidio-chat iframe",
                    'iframe[src*="tidio" i]'],
        truth_input=["textarea", '[contenteditable="true"]'],
        truth_send=['button[aria-label*="Send" i]', 'button[type="submit"]'],
        wait_ms=10000,
        notes="Widget lives in an iframe; launcher needs a click first.",
    ),
    Site(
        name="crisp",
        url="https://crisp.chat/en/",
        kind="widget",
        pre_clicks=['[aria-label="Open chat" i]', "[data-chat-status]"],
        truth_input=["textarea[name='message']", "textarea"],
        truth_send=['button[aria-label*="Send" i]', 'button[type="submit"]'],
        wait_ms=10000,
    ),
    Site(
        name="intercom",
        url="https://www.intercom.com",
        kind="widget",
        pre_clicks=['[aria-label*="Open Intercom" i]', ".intercom-launcher",
                    'iframe[name="intercom-launcher-frame"]'],
        truth_input=["textarea", '[contenteditable="true"]'],
        truth_send=['button[aria-label*="Send" i]'],
        wait_ms=15000,
    ),

    # ── Negative controls (engine must NOT claim a chat) ───────────────
    Site(name="google", url="https://www.google.com", kind="negative",
         notes="Search box. Engaging chat here is a false positive."),
    Site(name="bing", url="https://www.bing.com", kind="negative"),
    Site(name="wikipedia", url="https://en.wikipedia.org/wiki/Web_browser",
         kind="negative", notes="Article page with a search field."),
    Site(name="hackernews", url="https://news.ycombinator.com", kind="negative"),
    Site(name="bootstrap-forms",
         url="https://getbootstrap.com/docs/5.3/forms/form-control/",
         kind="negative", notes="Form-heavy docs page."),
    Site(name="github", url="https://github.com", kind="negative",
         notes="Search + marketing forms."),
]


def get_sites(names: list[str] | None = None, *,
              include_login: bool = False) -> list[Site]:
    sites = SITES
    if names:
        wanted = {n.strip() for n in names}
        unknown = wanted - {s.name for s in sites}
        if unknown:
            raise SystemExit(f"Unknown site(s): {', '.join(sorted(unknown))}")
        sites = [s for s in sites if s.name in wanted]
    if not include_login:
        sites = [s for s in sites if not s.login_required]
    return sites
