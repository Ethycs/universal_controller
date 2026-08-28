"""Site profiles — the Signature Lab's output schema (tier 3).

A profile is everything a cold client (or the health prober) needs to skip
discovery on a site: where the chat actually lives, how to open it, and
how fresh that intelligence is. See ``docs/01 - Design/01 - Signature
Lab.md`` for the design and its hard constraints — notably #1: profiles
are a warm-start cache, never a dependency; the generic engine must work
without them.

Storage: ``data/site_profiles.json`` (override ``UC_PROFILES_FILE``).
Served to clients by the status server at ``GET /signatures``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from uc_browser._paths import _SUBMODULE_ROOT

logger = logging.getLogger("uc_browser.site_profiles")

DEFAULT_TTL_S = 7 * 24 * 3600  # a week; redesigns are slower than that

FEED_VERSION = 1


def _profiles_file() -> Path:
    override = os.environ.get("UC_PROFILES_FILE")
    if override:
        return Path(override).resolve()
    return _SUBMODULE_ROOT / "data" / "site_profiles.json"


@dataclass
class SiteProfile:
    site: str                       # registry site name
    chat_page_url: str              # where the chat actually lives
    # Declarative steps to expose the composer, executed in order.
    # v1 vocabulary: {"type": "launcher-auto"} — run the generic
    # launcher-open heuristic; {"type": "click", "selector": "..."} —
    # click a specific element (frame-searched, best effort).
    pre_steps: list[dict] = field(default_factory=list)
    # URL of the frame the composer was found in ("" = main frame).
    frame_url_hint: str = ""
    # Best top-1 input score observed at verification time.
    detect_score: float = 0.0
    verify_level: str = "detect"    # detect | send
    verified_at: float = 0.0        # epoch seconds
    ttl_s: int = DEFAULT_TTL_S
    provenance: str = "lab-auto"    # lab-auto | analyst-agent | user
    notes: str = ""

    def is_fresh(self, now: Optional[float] = None) -> bool:
        return ((now or time.time()) - self.verified_at) < self.ttl_s


class ProfileStore:
    """Thread-safe JSON-backed store of site profiles."""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or _profiles_file()
        self._lock = threading.Lock()
        self._profiles: dict[str, SiteProfile] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for item in raw.get("profiles", []):
                p = SiteProfile(**item)
                self._profiles[p.site] = p
        except Exception as e:
            logger.warning("Could not load %s: %s", self._path, e)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.feed()
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def feed(self) -> dict:
        """The distribution shape served at GET /signatures."""
        return {
            "version": FEED_VERSION,
            "generated_at": time.time(),
            "profiles": [asdict(p) for p in self._profiles.values()],
        }

    def get(self, site: str) -> Optional[SiteProfile]:
        with self._lock:
            return self._profiles.get(site)

    def fresh(self, site: str) -> Optional[SiteProfile]:
        """Profile for ``site`` only if within TTL — expired profiles are
        advisory (design constraint #4) and callers must opt in via
        ``get`` to use one."""
        p = self.get(site)
        return p if p and p.is_fresh() else None

    def list(self) -> list[SiteProfile]:
        with self._lock:
            return sorted(self._profiles.values(), key=lambda p: p.site)

    def upsert(self, profile: SiteProfile) -> SiteProfile:
        profile.verified_at = profile.verified_at or time.time()
        with self._lock:
            self._profiles[profile.site] = profile
            self._save()
        return profile

    def remove(self, site: str) -> bool:
        with self._lock:
            if site in self._profiles:
                del self._profiles[site]
                self._save()
                return True
        return False


_store: Optional[ProfileStore] = None
_store_lock = threading.Lock()


def get_profile_store() -> ProfileStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ProfileStore()
        return _store
