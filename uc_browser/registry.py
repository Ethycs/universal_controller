"""Runtime site registry — the single source of truth for which sites UC
knows about, persisted as JSON so MCP tools / users can add sites without
code changes.

Distinct from ``bench/sites.py``: the bench registry carries ground-truth
*oracle selectors* for scoring and never leaves the bench. This registry
carries operational metadata: where the site lives, how often to probe
it, and which litellm model (if any) fronts it.

Storage: ``data/sites.json`` next to the submodule root (override with
``UC_SITES_FILE``). Built-in seeds are merged in on load; user entries
win on name collisions.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from uc_browser._paths import _SUBMODULE_ROOT

logger = logging.getLogger("uc_browser.registry")

DEFAULT_PROBE_INTERVAL_S = 15 * 60


def _sites_file() -> Path:
    override = os.environ.get("UC_SITES_FILE")
    if override:
        return Path(override).resolve()
    return _SUBMODULE_ROOT / "data" / "sites.json"


@dataclass
class SiteEntry:
    name: str
    url: str
    kind: str = "chat"                      # chat | widget
    notes: str = ""
    # litellm model that fronts this site, e.g. "uc/grok". None until a
    # driver (site adapter or verified generic path) exists — but see
    # auto-bootstrap: chat-kind sites are generically drivable as
    # uc/<name> even with this unset. Widget-kind sites are NEVER
    # auto-bootstrapped (their chats reach human support staff); they
    # require an explicit litellm_model to become callable.
    litellm_model: Optional[str] = None
    # Conventional-API fallback when this site is down/blocked: any model
    # litellm can route natively (e.g. "gpt-4o-mini", "xai/grok-3").
    # None → fall back to the UC_BACKUP_MODEL env default, if set.
    backup_model: Optional[str] = None
    login_required: bool = False
    probe_interval_s: int = DEFAULT_PROBE_INTERVAL_S
    # Deepest probe level to run: "reach" | "detect" | "send".
    # "send" performs a real round-trip and costs quota — opt in per site.
    probe_level: str = "detect"
    source: str = "user"                    # builtin | user
    added_at: float = field(default_factory=time.time)


# Sites UC ships knowing about. litellm_model where a hand-tuned adapter
# exists (grok) or the generic engine passes the bench on the site
# (bench/results/baseline.json) — those route through
# uc_browser.sites.generic. Widget sites whose chats reach human support
# staff (e.g. crisp.chat) are deliberately NOT given a model.
BUILTIN_SITES: list[SiteEntry] = [
    SiteEntry(name="grok", url="https://grok.com", litellm_model="uc/grok",
              source="builtin",
              notes="Full driver (grok_fast + GrokClient)."),
    SiteEntry(name="grok-generic", url="https://grok.com",
              litellm_model="uc/grok-generic", source="builtin",
              notes="grok.com through the pure generic engine — adapter "
                    "bypassed. Exists to verify generalization; same site "
                    "as 'grok'."),
    SiteEntry(name="chatgpt", url="https://chatgpt.com",
              litellm_model="uc/chatgpt", source="builtin",
              notes="Generic engine; bench input_pipeline pass."),
    SiteEntry(name="perplexity", url="https://www.perplexity.ai",
              litellm_model="uc/perplexity", source="builtin",
              notes="Generic engine; bench input_pipeline pass."),
    SiteEntry(name="pi", url="https://pi.ai/talk",
              litellm_model="uc/pi", source="builtin",
              notes="Generic engine; bench input_pipeline pass."),
    SiteEntry(name="lmarena", url="https://lmarena.ai",
              litellm_model="uc/lmarena", source="builtin",
              notes="Generic engine; bench input_pipeline pass."),
    SiteEntry(name="claude", url="https://claude.ai", login_required=True,
              source="builtin"),
    SiteEntry(name="gemini", url="https://gemini.google.com/app",
              login_required=True, source="builtin"),
    SiteEntry(name="deepseek", url="https://chat.deepseek.com",
              login_required=True, source="builtin"),
    SiteEntry(name="copilot", url="https://copilot.microsoft.com",
              login_required=True, source="builtin"),
    SiteEntry(name="huggingchat", url="https://huggingface.co/chat/",
              login_required=True, source="builtin"),
]

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")


class SiteRegistry:
    """Thread-safe JSON-backed site registry."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or _sites_file()
        self._lock = threading.Lock()
        self._user: dict[str, SiteEntry] = {}
        self._load()

    # ── persistence ─────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw.get("sites", []):
                entry = SiteEntry(**item)
                self._user[entry.name] = entry
        except Exception as e:
            logger.warning("Could not load %s: %s", self._path, e)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sites": [asdict(e) for e in self._user.values()]}
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # ── queries ─────────────────────────────────────────────────────

    def list(self) -> list[SiteEntry]:
        with self._lock:
            merged: dict[str, SiteEntry] = {e.name: e for e in BUILTIN_SITES}
            merged.update(self._user)  # user entries win
            return sorted(merged.values(), key=lambda e: e.name)

    def get(self, name: str) -> Optional[SiteEntry]:
        for e in self.list():
            if e.name == name:
                return e
        return None

    def search(self, query: str) -> list[SiteEntry]:
        q = query.strip().lower()
        if not q:
            return self.list()
        hits = []
        for e in self.list():
            haystack = " ".join([e.name, e.url, e.notes, e.kind]).lower()
            if q in haystack:
                hits.append(e)
        return hits

    # ── mutation ────────────────────────────────────────────────────

    def add(self, name: str, url: str, *, kind: str = "chat", notes: str = "",
            probe_interval_s: int = DEFAULT_PROBE_INTERVAL_S,
            probe_level: str = "detect",
            login_required: bool = False) -> SiteEntry:
        name = name.strip().lower()
        if not _NAME_RE.match(name):
            raise ValueError(
                f"Invalid site name {name!r}: lowercase letters, digits, "
                "hyphen/underscore, 2-41 chars.")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid url {url!r}: must be http(s).")
        if kind not in ("chat", "widget"):
            raise ValueError(f"kind must be 'chat' or 'widget', got {kind!r}")
        if probe_level not in ("reach", "detect", "send"):
            raise ValueError("probe_level must be reach|detect|send")
        entry = SiteEntry(
            name=name, url=url, kind=kind, notes=notes,
            probe_interval_s=max(60, int(probe_interval_s)),
            probe_level=probe_level, login_required=login_required,
            source="user",
        )
        with self._lock:
            self._user[name] = entry
            self._save()
        return entry

    def remove(self, name: str) -> bool:
        """Remove a user-added site. Builtins cannot be removed."""
        with self._lock:
            if name in self._user:
                del self._user[name]
                self._save()
                return True
        return False


def bootstrap_enabled() -> bool:
    """Auto-bootstrap: verified chat-kind sites become callable uc/<name>
    models without explicit wiring. On by default; UC_AUTO_BOOTSTRAP=0
    turns it off."""
    return os.environ.get("UC_AUTO_BOOTSTRAP", "1") != "0"


def advertised_model(entry: SiteEntry) -> tuple[Optional[str], bool]:
    """(model_name, bootstrapped) this entry presents to API clients.

    Explicit litellm_model wins. Otherwise chat-kind sites bootstrap to
    uc/<name> when enabled. Widget-kind sites never bootstrap — their
    chats reach human support staff, so callability is an explicit
    decision, not an inference.
    """
    if entry.litellm_model:
        return entry.litellm_model, False
    if bootstrap_enabled() and entry.kind == "chat":
        return f"uc/{entry.name}", True
    return None, False


_registry: Optional[SiteRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> SiteRegistry:
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = SiteRegistry()
        return _registry
