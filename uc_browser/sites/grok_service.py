"""Grok adapter for service sessions: one borrowed page, optionally one folder.

The service owns the browser, the session thread and the page. This adapter
only *drives* that page (``GrokSessionAdapter(runtime, page, ...)``): it never
starts or closes a browser, a context, Playwright or the page, and it does not
use the process singletons of :mod:`uc_browser.sites.grok`. Page-only controls
come from ``runtime.receiver()`` (an unstarted ``UCBrowser``); the engine is
placed by ``runtime.ensure_engine``.

Folder scoping
--------------
With ``folder_url`` set (a Grok *project* page) the adapter is fail-closed:

* new conversations are started from the project's own composer, never from
  the account home page;
* before anything is typed the page must show a visible, enabled composer on
  the intended surface. A deviation is classified from structure (sign-in
  links, password fields, an auth route or a sign-in dialog => ``auth_required``;
  anything else => ``page_deviation``) and the operation stops. There is no
  phrase list and no wait that falls through to typing anyway;
* an explicit conversation is acted on only after verification against the
  project's authenticated conversation listing, otherwise ``folder_mismatch``;
* listing and ``cleanup_threads`` use the workspace-filtered API, never the
  account-wide sidebar. Every returned row must identify that workspace.

Nothing here logs prompts, replies, titles or page text.

Live-verified on 2026-11-24: the project has an enabled composer, new chats keep
``/project/<id>?chat=<id>``, and its conversation cards have no links. Project
identity and membership therefore use the same authenticated workspace API
that the page requests, not a guessed DOM list.
"""

from __future__ import annotations

import inspect
import logging
import math
import time
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import Error as PlaywrightError

from uc_browser.service.errors import ServiceError
from uc_browser.sites.grok import (
    _CLICK_STOP_JS,
    _SELECTORS,
    GrokClient,
    GrokPreSubmitError,
    GrokSubmitUncertain,
    _conv_id_from_url,
    _normalize_url,
)
from uc_browser.sites.grok_fast import GrokFastClient

log = logging.getLogger("uc_browser.sites.grok_service")

SITE = "grok"

#: The service-visible operations, in documentation order. A name is callable
#: only if it is listed here and has a ``_op_<name>`` method.
OPERATIONS = (
    "send",
    "read",
    "list_conversations",
    "delete",
    "rename",
    "archive",
    "regenerate",
    "stop",
    "get_models",
    "switch_model",
    "new_chat",
    "cleanup_threads",
)

DEFAULT_SEND_TIMEOUT_S = 300
DEFAULT_REGENERATE_TIMEOUT_S = 60
HYDRATE_S = 35.0  # grok.com sits on a loading splash for 15-25 s
PROBE_INTERVAL_MS = 500
NAV_TIMEOUT_MS = 30000
MISMATCH_PROBES = 6  # consecutive probes on a wrong-but-working page before giving up
MAX_TITLE_CHARS = 200


# The page's own session key inside the borrowed-page GrokClient (unused for
# routing: every key maps to the one page).
_SESSION_KEY = "service"


class GrokServiceError(ServiceError):
    """A ``ServiceError`` that also carries structured context in ``data``."""

    def __init__(self, code: str, detail: str, status: int = 500, **data):
        super().__init__(code, detail, status)
        self.data = data


def _fail(code: str, detail: str, status: int, **data) -> GrokServiceError:
    return GrokServiceError(code, detail, status, **data)


# ── structural page probes (run in the page) ─────────────────────────────

_SURFACE_JS = r"""(args) => {
  const composerSel = args[0];
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const all = Array.from(document.querySelectorAll(composerSel));
  const c = all.filter(shown)[0] || null;
  const enabled = !!c
    && c.getAttribute('contenteditable') !== 'false'
    && c.getAttribute('aria-disabled') !== 'true'
    && !c.closest('[disabled], [aria-disabled="true"], fieldset:disabled');
  const AUTH_PATH = /^\/(?:sign-?in|sign-?up|log-?in|signup|login|auth|oauth2?)(?:\/|$)/i;
  const AUTH_HREF = /\/(?:sign-?in|sign-?up|log-?in|signup|login|oauth2?)(?:[\/?#]|$)|^https?:\/\/accounts\.x\.ai\b|^https?:\/\/(?:x|twitter)\.com\/i\/(?:oauth|flow)/i;
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    if (!shown(a)) continue;
    let href;
    try { href = new URL(a.getAttribute('href') || '', location.href).href; }
    catch (e) { continue; }
    if (AUTH_HREF.test(href)) links.push(a);
  }
  const fields = Array.from(document.querySelectorAll(
    'input[type="password"], input[autocomplete="current-password"], '
    + 'input[autocomplete="username"], input[autocomplete="webauthn"]'
  )).filter(shown);
  const dlg = '[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open]';
  const dialog = links.concat(fields).some((el) => !!el.closest(dlg));
  return {
    url: location.href,
    composer: {present: all.length > 0, visible: !!c, enabled: enabled},
    auth: {route: AUTH_PATH.test(location.pathname),
           links: links.length, fields: fields.length, dialog: dialog},
  };
}"""

_PROJECT_GET_JS = r"""async (path) => {
  const response = await fetch(path, {
    credentials: 'include',
    signal: AbortSignal.timeout(30000)
  });
  let data = null;
  try { data = await response.json(); } catch {}
  return {status: response.status, data};
}"""


def classify_surface(state: dict, *, identity_ok: bool) -> str:
    """``"ready"``, ``"auth_required"`` or ``"page_deviation"`` for a probe.

    Structural only: an auth route or a sign-in dialog is conclusive;
    otherwise the surface is ready when the composer is visible and enabled on
    the intended page, and if it is not, sign-in links or credential fields
    mean the session is signed out. Everything else is a deviation.
    """
    composer = state.get("composer") or {}
    auth = state.get("auth") or {}
    if auth.get("route") or auth.get("dialog"):
        return "auth_required"
    if composer.get("visible") and composer.get("enabled") and identity_ok:
        return "ready"
    if auth.get("links") or auth.get("fields"):
        return "auth_required"
    return "page_deviation"


class _Streamed:
    """Wrap ``on_delta`` to learn what was emitted, then reconcile the rest."""

    def __init__(self, sink: Optional[Callable[[str], None]]):
        self.sink = sink
        self.emitted = ""

    def __call__(self, delta: str) -> None:
        self.emitted += delta
        if self.sink is not None:
            self.sink(delta)

    def finish(self, text: str) -> None:
        """Emit any not-yet-streamed remainder of ``text`` (REST reads are whole)."""
        if self.sink is None or not text or not text.startswith(self.emitted):
            return
        rest = text[len(self.emitted):]
        if rest:
            self.emitted = text
            try:
                self.sink(rest)
            except Exception:  # noqa: BLE001 - a dead sink never breaks the send
                pass


# ── argument validation ──────────────────────────────────────────────────


def _bad(detail: str) -> GrokServiceError:
    return _fail("bad_arguments", detail, 400)


def _text_arg(name: str, value, *, limit: int | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{name} must be a non-empty string")
    if limit is not None and len(value.strip()) > limit:
        raise _bad(f"{name} is longer than {limit} characters")
    return value


def _timeout_arg(value, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise _bad("timeout_s must be a positive number")
    return value


def _bool_arg(name: str, value) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"{name} must be a boolean")
    return value


class GrokSessionAdapter:
    """Drive one borrowed Grok page for a service session.

    Construct and use on the page's own session thread. ``runtime`` is that
    thread's ``Runtime``; ``page`` is the session's page. ``folder`` names the
    folder for results and ``folder_url`` is its project page; both are
    deployment configuration, not defaults. Without ``folder_url`` the adapter
    works on the whole account (single-conversation operations only).

    The adapter keeps the set of conversations it has verified as folder
    members; keep one adapter per page so that set survives between calls.
    """

    def __init__(self, runtime, page, *, folder: Optional[str] = None, folder_url: Optional[str] = None):
        if folder is not None and (not isinstance(folder, str) or not folder.strip()):
            raise ValueError("folder must be a non-empty string")
        if folder and not folder_url:
            raise ValueError("a folder needs a folder_url to route and verify against")
        self.folder = folder.strip() if folder else None
        self.folder_url: Optional[str] = None
        self._scoped = folder_url is not None
        self._host = "grok.com"
        self._folder_path = ""
        if folder_url is not None:
            parts = urlsplit(folder_url)
            path = parts.path.rstrip("/")
            if (parts.scheme != "https" or not parts.hostname or parts.username
                    or parts.password or not path):
                raise ValueError("folder_url must be an https URL of a project page")
            self._host = parts.hostname.lower()
            self._folder_path = path
            self.folder_url = f"https://{parts.netloc}{path}"
        self._home = self.folder_url or "https://grok.com/"
        self._runtime = runtime
        self._page = page
        self._uc = runtime.receiver()  # also enforces the owning thread
        self._client = GrokClient.borrowing(self._uc, page, home_url=self.folder_url)
        self._fast = GrokFastClient(grok_client=self._client)
        self._members: dict[str, str] = {}  # conversation id -> canonical URL
        self.hydrate_s = HYDRATE_S

    # ── public surface ───────────────────────────────────────────────

    @property
    def url(self) -> Optional[str]:
        """The URL of the conversation the page is on, else ``None`` (unchanged).

        In a folder this is the actual project URL when the page has one,
        otherwise the canonical project form of the conversation, so a stored
        session URL can be re-verified against the folder later.
        """
        raw = self._page.url
        cid = _conv_id_from_url(raw)
        if cid is None or not self._host_ok(raw):
            return None
        return self._public_url(cid.lower(), raw)

    @staticmethod
    def stop_page(page) -> bool:
        """Click Grok's stop control on ``page``; True if one was clicked.

        Needs no adapter state and takes no lock, so a control worker can run
        it against a session page that a send is currently waiting on.
        """
        return bool(page.evaluate(_CLICK_STOP_JS, _SELECTORS["stop_button"]))

    def execute(self, op: str, kwargs: Optional[dict] = None, on_delta: Optional[Callable[[str], None]] = None) -> Any:
        """Run ``op`` with JSON ``kwargs``; the result is a JSON-serialisable dict."""
        if op not in OPERATIONS:
            raise _fail("no_such_op", f"grok has no operation {op!r}", 404)
        kwargs = dict(kwargs or {})
        if "on_delta" in kwargs:
            raise _bad("on_delta is not a wire argument")
        handler = getattr(self, f"_op_{op}")
        call = dict(kwargs)
        if op == "send":
            call["on_delta"] = on_delta
        try:
            inspect.signature(handler).bind(**call)
        except TypeError as exc:
            raise _bad(f"{op}: {exc}") from None
        return handler(**call)

    # ── URL identity ─────────────────────────────────────────────────

    def _host_ok(self, url: str) -> bool:
        try:
            host = urlsplit(url).hostname
        except ValueError:
            return False
        return bool(host) and host.lower() == self._host

    def _in_folder(self, url: str) -> bool:
        if not self._scoped or not self._host_ok(url):
            return False
        path = urlsplit(url).path.rstrip("/")
        return path == self._folder_path or path.startswith(self._folder_path + "/")

    def _is_landing(self, url: str) -> bool:
        """The folder's (or home's) new-chat page: right path, no chat selected."""
        if not self._host_ok(url) or _conv_id_from_url(url):
            return False
        return urlsplit(url).path.rstrip("/") == self._folder_path

    def _is_conversation(self, url: str, cid: str) -> bool:
        found = _conv_id_from_url(url)
        if not found or found.lower() != cid or not self._host_ok(url):
            return False
        if not self._scoped:
            return True
        return self._in_folder(url) or urlsplit(url).path.startswith("/c/")

    def _on_surface(self, url: str) -> bool:
        """Is ``url`` somewhere this adapter may act (folder, or a verified member)?"""
        if not self._host_ok(url):
            return False
        if not self._scoped:
            return True
        cid = _conv_id_from_url(url)
        if cid is not None:
            return cid.lower() in self._members
        return self._in_folder(url)

    def _canonical(self, cid: str) -> str:
        if self._scoped:
            return f"{self.folder_url}?chat={cid}"
        return f"https://{self._host}/c/{cid}"

    def _public_url(self, cid: str, raw: str) -> str:
        """The URL to hand back to callers: the actual project URL when the page
        has one, else the canonical project form (re-verifiable later)."""
        if self._scoped and not (self._in_folder(raw) and _conv_id_from_url(raw)):
            return self._canonical(cid)
        return raw

    def _scope(self) -> dict:
        return {"folder": self.folder, "folder_url": self.folder_url} if self._scoped else {}

    def _is_new_chat_url(self, value) -> bool:
        """A conversation-less landing URL (a stored "new chat" marker), not a conversation."""
        if not isinstance(value, str):
            return False
        url = value.strip()
        if not url.startswith(("http://", "https://")) or _conv_id_from_url(url) or not self._host_ok(url):
            return False
        return urlsplit(url).path.rstrip("/") in ("", self._folder_path)

    def _parse_conversation(self, value) -> tuple[str, str]:
        """``(conversation id, url)`` from a URL or bare id; ``bad_conversation`` otherwise."""
        _text_arg("conversation_url", value)
        url = _normalize_url(value.strip())
        cid = _conv_id_from_url(url)
        if cid is None or not self._host_ok(url):
            raise _fail("bad_conversation", "conversation_url must name one Grok conversation", 400)
        return cid.lower(), url

    # ── page surface ─────────────────────────────────────────────────

    def _live_page(self):
        if self._page.is_closed():
            raise _fail("session_page_closed", "the session's page is closed", 409)
        return self._page

    def _probe(self, page) -> dict:
        try:
            return page.evaluate(_SURFACE_JS, [_SELECTORS["chat_input"]]) or {}
        except PlaywrightError:
            if page.is_closed():
                raise
            return {}  # navigation in flight: probe again

    def _surface_error(self, verdict: str, state: dict) -> GrokServiceError:
        path = urlsplit(state.get("url") or self._page.url).path or "/"
        if verdict == "auth_required":
            return _fail(
                "auth_required",
                "Grok shows sign-in controls instead of the expected page; "
                "sign in on this window (cookie import) and retry",
                401, path=path,
            )
        composer = state.get("composer") or {}
        return _fail(
            "page_deviation",
            "the page is not the expected Grok surface with an enabled composer "
            f"(path={path}, composer_visible={bool(composer.get('visible'))}, "
            f"composer_enabled={bool(composer.get('enabled'))})",
            502, path=path,
        )

    def _await_surface(self, page, expect: Callable[[str], bool]) -> dict:
        """Wait until the intended surface shows an enabled composer, or raise.

        Every probe is classified; a conclusive auth or deviation verdict ends
        the wait early, and expiry raises the last verdict. Never falls through.
        """
        deadline = time.monotonic() + self.hydrate_s
        auth_streak = mismatch_streak = 0
        while True:
            state = self._probe(page)
            identity = bool(expect(state.get("url") or page.url))
            verdict = classify_surface(state, identity_ok=identity)
            if verdict == "ready":
                return state
            composer = state.get("composer") or {}
            auth = state.get("auth") or {}
            if verdict == "auth_required":
                auth_streak += 1
                if auth.get("route") or auth.get("dialog") or auth_streak >= 2:
                    raise self._surface_error(verdict, state)
            else:
                auth_streak = 0
            if composer.get("visible") and composer.get("enabled") and not identity:
                mismatch_streak += 1
                if mismatch_streak >= MISMATCH_PROBES:
                    raise self._surface_error(verdict, state)
            else:
                mismatch_streak = 0
            if time.monotonic() >= deadline:
                raise self._surface_error(verdict, state)
            page.wait_for_timeout(PROBE_INTERVAL_MS)

    def _goto(self, page, url: str) -> None:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except PlaywrightError as exc:
            if page.is_closed():
                raise
            raise _fail("navigation_failed", "page navigation failed", 502) from exc

    def _tidy(self, page) -> None:
        """Dismiss cookie banners/modals after a navigation (cosmetic, best effort)."""
        try:
            self._uc.dismiss_cookies(page)
            self._uc.close_modal(page)
        except Exception as exc:  # noqa: BLE001
            if page.is_closed():
                raise
            log.debug("page tidy failed: %s", type(exc).__name__)

    def _open_surface(self, url: str, expect: Callable[[str], bool]):
        """Put the page on ``url``, verify the surface, prepare the engine.

        Navigates only when the page is not already there (the ``chat``
        parameter counts), so a conversation switch inside one project is a
        real navigation. Returns the page once an enabled composer is visible
        on an identity-checked URL.
        """
        page = self._live_page()
        navigated = not GrokClient._same_path(page.url, url)
        if navigated:
            self._goto(page, url)
        self._await_surface(page, expect)
        if self._scoped:
            self._verify_project(page)
        engine_ready = self._runtime.ensure_engine(page)
        if navigated and engine_ready:
            self._tidy(page)
        return page

    def _dismiss_popover(self, page) -> None:
        try:
            page.keyboard.press("Escape")
        except PlaywrightError:
            pass

    # ── folder membership ────────────────────────────────────────────

    def _project_json(self, page, path: str) -> dict:
        result = page.evaluate(_PROJECT_GET_JS, path)
        status = result.get("status") if isinstance(result, dict) else None
        if status == 401:
            raise _fail("auth_required", "Grok refused the project request: HTTP 401", 401)
        data = result.get("data") if isinstance(result, dict) else None
        if status != 200 or not isinstance(data, dict):
            raise _fail("page_deviation", f"Grok project API returned an unexpected response (HTTP {status})", 502)
        return data

    def _verify_project(self, page) -> None:
        workspace = self._folder_path.rsplit("/", 1)[-1]
        data = self._project_json(page, f"/rest/workspaces/{workspace}")
        if data.get("workspaceId") != workspace or (self.folder and data.get("name") != self.folder):
            raise _fail("folder_mismatch", "the configured project identity or name does not match Grok", 409)

    def _project_listing(self, page) -> list[dict]:
        workspace = self._folder_path.rsplit("/", 1)[-1]
        token = ""
        seen_tokens: set[str] = set()
        rows: dict[str, dict] = {}
        while True:
            query = {"pageSize": 60, "workspaceId": workspace}
            if token:
                query["pageToken"] = token
            data = self._project_json(page, "/rest/app-chat/conversations?" + urlencode(query))
            batch = data.get("conversations")
            if not isinstance(batch, list):
                raise _fail("page_deviation", "Grok's project conversation list is missing", 502)
            for row in batch:
                if not isinstance(row, dict) or row.get("workspaceId") != workspace:
                    raise _fail("folder_mismatch", "Grok returned a conversation outside the configured project", 409)
                cid = row.get("conversationId")
                if not isinstance(cid, str) or not cid:
                    raise _fail("page_deviation", "Grok returned a conversation without an id", 502)
                rows[cid.lower()] = {
                    "id": cid.lower(), "title": row.get("title"),
                    "url": self._canonical(cid.lower()),
                }
            token = data.get("nextPageToken") or ""
            if not token:
                return list(rows.values())
            if not isinstance(token, str) or token in seen_tokens:
                raise _fail("page_deviation", "Grok repeated or malformed its project pagination token", 502)
            seen_tokens.add(token)

    def _scan_folder(self) -> list[dict]:
        """The project's conversation list, newest first as Grok's API orders it."""
        if not self._scoped:
            raise RuntimeError("_scan_folder requires a configured folder")  # programming error
        page = self._open_surface(self._home, self._is_landing)
        out = self._project_listing(page)
        self._members = {row["id"]: row["url"] for row in out}
        return out

    def _require_member(self, cid: str, given_url: str, *, fresh: bool) -> str:
        """Return the URL to act on for ``cid`` after verifying folder membership.

        Unscoped adapters trust the caller's URL. Scoped ones need the id in
        the verified set (``fresh=False``) or in a listing taken now
        (``fresh=True``, for destructive operations); otherwise
        ``folder_mismatch``.
        """
        if not self._scoped:
            return given_url
        if not fresh and cid in self._members:
            return self._members[cid]
        if cid in {item["id"] for item in self._scan_folder()}:
            return self._members[cid]
        raise _fail(
            "folder_mismatch",
            "the conversation is not listed in the configured folder",
            409, conversation_id=cid,
        )

    def _to_conversation(self, cid: str, given_url: str, *, fresh: bool = True):
        target = self._require_member(cid, given_url, fresh=fresh)
        return self._open_surface(target, lambda u: self._is_conversation(u, cid))

    def _prepare_row_op(self, cid: str, given_url: str) -> None:
        """Leave the page on a verified surface where ``cid``'s row can be acted on.

        Scoped: a fresh folder listing must contain ``cid`` (the page is then on
        the folder page). Unscoped: the diagnosed account home.
        """
        if self._scoped:
            self._require_member(cid, given_url, fresh=True)
        else:
            self._open_surface(self._home, self._is_landing)

    # ── operations ───────────────────────────────────────────────────

    def _op_send(
        self,
        message,
        conversation_url=None,
        timeout_s=None,
        wait_for_response=True,
        on_delta=None,
    ) -> dict:
        _text_arg("message", message)
        timeout = _timeout_arg(timeout_s, DEFAULT_SEND_TIMEOUT_S)
        wait = _bool_arg("wait_for_response", wait_for_response)
        if conversation_url and not self._is_new_chat_url(conversation_url):
            cid, given = self._parse_conversation(conversation_url)
            page = self._to_conversation(cid, given)
        else:
            # No conversation (or a stored new-chat URL): start one in the
            # folder. A scoped adapter never opens the account home to do so.
            cid = None
            page = self._open_surface(self._home, self._is_landing)
        # The page is verified and already there, so the inner clients never
        # navigate (and never drift to the account home).
        here = page.url
        streamed = _Streamed(on_delta)
        try:
            if not wait:
                result = self._client.send(
                    message, conversation_url=here, timeout_s=timeout,
                    wait_for_response=False, session_key=_SESSION_KEY,
                )
            else:
                try:
                    result = self._fast.send(
                        message, conversation_url=here, session_key=_SESSION_KEY,
                        timeout_s=timeout, on_delta=streamed,
                    )
                except GrokPreSubmitError as exc:
                    # Proven not submitted: the only case a re-send is safe.
                    log.warning("fast send failed before submit (%s); using the DOM path", exc)
                    result = self._client.send(
                        message, conversation_url=here, timeout_s=timeout,
                        wait_for_response=True, session_key=_SESSION_KEY,
                    )
        except GrokSubmitUncertain as exc:
            raise _fail("submit_uncertain", str(exc), 502, url=self.url) from None
        return self._finish_send(result, streamed)

    def _finish_send(self, result: dict, streamed: _Streamed) -> dict:
        raw = self._page.url
        cid = (result.get("conversation_id") or _conv_id_from_url(raw) or "").lower() or None
        text = result.get("response") or ""
        payload = {"response": text, "url": result.get("url") or raw, "conversation_id": cid}
        if cid:
            payload["url"] = self._public_url(cid, raw)
            if self._scoped:
                if not self._in_folder(raw):
                    # Verify without navigating: a blind send may still be generating.
                    self._verify_project(self._page)
                    if cid not in {row["id"] for row in self._project_listing(self._page)}:
                        raise _fail(
                            "folder_mismatch",
                            "the submitted conversation is not in the configured project; it was not resent",
                            409, url=raw,
                        )
                self._members[cid] = self._canonical(cid)
                payload["folder_verified"] = True
        if payload["url"] != raw:
            payload["page_url"] = raw
        payload.update(self._scope())
        streamed.finish(text)
        if result.get("timed_out"):
            raise _fail(
                "timeout",
                text or "timed out before any reply text rendered",
                504, partial=text, result=payload,
            )
        return payload

    def _op_read(self, conversation_url) -> dict:
        cid, given = self._parse_conversation(conversation_url)
        page = self._to_conversation(cid, given)
        out = self._client.read(page.url)
        if (out.get("conversation_id") or "").lower() != cid:
            raise self._surface_error("page_deviation", {"url": page.url})
        return {**out, "url": self._public_url(cid, out.get("url") or page.url),
                "conversation_id": cid, **self._scope()}

    def _op_list_conversations(self) -> list:
        if self._scoped:
            return self._scan_folder()
        self._open_surface(self._home, self._is_landing)
        return self._client.list_conversations()

    def _delete_verified(self, cid: str) -> bool:
        page = self._live_page()
        deleted = bool(self._client.delete(cid))
        if deleted:
            self._members.pop(cid, None)
        else:
            self._dismiss_popover(page)
        return deleted

    def _op_delete(self, conversation_url) -> bool:
        cid, given = self._parse_conversation(conversation_url)
        self._prepare_row_op(cid, given)
        return self._delete_verified(cid)

    def _op_rename(self, conversation_url, new_title) -> bool:
        cid, given = self._parse_conversation(conversation_url)
        _text_arg("new_title", new_title, limit=MAX_TITLE_CHARS)
        self._prepare_row_op(cid, given)
        page = self._live_page()
        renamed = bool(self._client.rename(cid, new_title.strip()))
        if not renamed:
            self._dismiss_popover(page)
        return renamed

    def _op_archive(self, conversation_url) -> bool:
        cid, given = self._parse_conversation(conversation_url)
        self._prepare_row_op(cid, given)
        page = self._live_page()
        archived = bool(self._client.archive(cid))
        if not archived:
            self._dismiss_popover(page)
        return archived

    def _op_regenerate(self, conversation_url, timeout_s=None) -> dict:
        cid, given = self._parse_conversation(conversation_url)
        timeout = _timeout_arg(timeout_s, DEFAULT_REGENERATE_TIMEOUT_S)
        page = self._to_conversation(cid, given)
        result = self._client.regenerate(page.url, timeout_s=timeout)
        payload = {**result, "conversation_id": cid,
                   "url": self._public_url(cid, self._page.url), **self._scope()}
        if result.get("reason") == "timeout":
            text = result.get("response") or ""
            raise _fail("timeout", text or "timed out before the regenerated reply rendered",
                        504, partial=text, result=payload)
        return payload

    def _op_stop(self, conversation_url=None) -> bool:
        page = self._live_page()
        if conversation_url:
            cid, given = self._parse_conversation(conversation_url)
            target = self._require_member(cid, given, fresh=False)
            if not GrokClient._same_path(page.url, target):
                page = self._open_surface(target, lambda u: self._is_conversation(u, cid))
        elif not self._on_surface(page.url):
            raise _fail("folder_mismatch", "the session page is outside the configured folder", 409)
        stopped = self.stop_page(page)
        return bool(stopped)

    def _op_get_models(self) -> list:
        self._open_surface(self._home, self._is_landing)
        return self._client.get_models()

    def _op_switch_model(self, model_name) -> bool:
        _text_arg("model_name", model_name)
        self._open_surface(self._home, self._is_landing)
        return bool(self._client.switch_model(model_name))

    def _op_new_chat(self) -> dict:
        page = self._open_surface(self._home, self._is_landing)
        return {"url": page.url, "conversation_id": None, **self._scope()}

    def _op_cleanup_threads(self, keep_recent) -> dict:
        if isinstance(keep_recent, bool) or not isinstance(keep_recent, int) or keep_recent < 0:
            raise _bad("keep_recent must be a non-negative integer")
        if not self._scoped:
            raise _fail(
                "folder_required",
                "bulk cleanup needs a configured folder; refusing to enumerate the whole account",
                409,
            )
        items = self._scan_folder()
        keep = min(keep_recent, len(items))
        deleted_ids: list[str] = []
        failed = 0
        page = self._live_page()
        for item in items[keep:]:
            try:
                if self._delete_verified(item["id"]):
                    deleted_ids.append(item["id"])
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001 - one row failing must not stop the sweep
                if page.is_closed():
                    raise
                log.warning("cleanup_threads: delete failed (%s)", type(exc).__name__)
                failed += 1
        return {
            "summary": (f"cleanup_threads: kept {keep}, deleted {len(deleted_ids)}, "
                        f"failed {failed}, total_seen {len(items)}"),
            "deleted_ids": deleted_ids,
        }
