"""Map CDP target ids to the Playwright ``Page`` objects of one attachment.

Loaded only by ``UCBrowser`` (it needs Playwright; ``client.py`` must not).

A ``TargetResolver`` belongs to the thread whose Playwright instance produced
its context: sync Playwright objects are never touched from another thread.

Two measured facts shape it (Build Spec §4, §6; Analysis rows 6 and 11):

* A page the service opened is not on the client's connection yet when the
  HTTP call returns (visible after ~0.16 s), so ``resolve`` polls, up to 10 s.
* ``context.pages`` goes stale until the connection makes a real call (sync
  Playwright dispatches incoming events only inside a call), so every scan
  first pumps events with ``wait_for_timeout(0)`` on a live page and only then
  reads ``pages``.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from playwright.sync_api import Error as PlaywrightError

from uc_browser.service.errors import ServiceError

logger = logging.getLogger("uc_browser")

DEFAULT_WAIT_S = 10.0
DEFAULT_POLL_S = 0.05


class TargetNotFound(ServiceError, LookupError):
    """The wanted target id never appeared on this attachment."""

    def __init__(self, target_id: str, waited_s: float):
        super().__init__(
            "no_such_target",
            f"target {target_id} did not appear on this connection within {waited_s:g}s",
            404,
        )
        self.target_id = target_id


class TargetResolver:
    def __init__(self, context, *, wait_s: float = DEFAULT_WAIT_S, poll_s: float = DEFAULT_POLL_S):
        self.context = context
        self.wait_s = wait_s
        self.poll_s = poll_s
        self._by_page: dict = {}  # Page -> target id (forward)
        self._by_id: dict = {}    # target id -> Page (reverse)

    # ── events ──────────────────────────────────────────────────────

    def _anchor(self):
        for page in list(self._by_page):
            if not page.is_closed():
                return page
        for page in list(self.context.pages):
            if not page.is_closed():
                return page
        return None

    def pump(self, ms: int = 0) -> None:
        """Make one real Playwright call so queued events are dispatched;
        with ``ms > 0`` it also waits that long (events keep flowing)."""
        anchor = self._anchor()
        try:
            if anchor is not None:
                anchor.wait_for_timeout(ms)
                return
            self.context.cookies()  # no page to wait on: any round trip drains events
        except PlaywrightError as exc:
            logger.debug("target pump failed: %s", exc)
        if ms:
            time.sleep(ms / 1000.0)

    # ── cache ───────────────────────────────────────────────────────

    def _remember(self, page, target_id: str) -> None:
        self._by_page[page] = target_id
        self._by_id[target_id] = page

    def _evict(self, page) -> None:
        target_id = self._by_page.pop(page, None)
        if target_id is not None and self._by_id.get(target_id) is page:
            del self._by_id[target_id]

    def _probe(self, page) -> str:
        session = self.context.new_cdp_session(page)
        try:
            return session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        finally:
            try:
                session.detach()
            except PlaywrightError:
                pass

    def scan(self) -> dict:
        """Pump, then return ``{target_id: Page}`` for every live page,
        identifying new ones and evicting closed or vanished ones."""
        self.pump(0)
        live = [p for p in list(self.context.pages) if not p.is_closed()]
        live_set = set(live)
        for page in [p for p in self._by_page if p not in live_set]:
            self._evict(page)
        for page in live:
            if page in self._by_page:
                continue
            try:
                self._remember(page, self._probe(page))
            except PlaywrightError as exc:
                # Closed between listing and probing, or not identifiable yet:
                # the next scan decides.
                logger.debug("getTargetInfo failed for %s: %s", page.url, exc)
        return dict(self._by_id)

    # ── lookups ─────────────────────────────────────────────────────

    def target_id(self, page) -> str:
        """The target id of ``page``. An unknown page is probed directly, so a
        closed page raises Playwright's own error rather than a made-up one."""
        cached = self._by_page.get(page)
        if cached is not None:
            if not page.is_closed():
                return cached
            self._evict(page)
        target_id = self._probe(page)
        self._remember(page, target_id)
        return target_id

    def resolve(self, target_id: str, *, timeout: Optional[float] = None):
        """The ``Page`` with this target id, waiting for it to appear."""
        wait_s = self.wait_s if timeout is None else timeout
        deadline = time.monotonic() + wait_s
        while True:
            page = self._by_id.get(target_id)
            if page is not None and not page.is_closed():
                return page
            if page is not None:
                self._evict(page)
            page = self.scan().get(target_id)
            if page is not None:
                return page
            if time.monotonic() >= deadline:
                raise TargetNotFound(target_id, wait_s)
            self.pump(max(1, int(self.poll_s * 1000)))
