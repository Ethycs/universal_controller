"""litellm CustomLLM that drives browser-automated chats (Grok today).

Register once with :func:`register_uc_provider`, then call litellm with
``model="uc/grok"``. Grok is a thin client of the UC service's
``/sites/grok`` routes: the service owns the browser, the conversation map
and the Grok folder policy; this process holds no browser. The service
address is explicit — ``UCBrowserCustomLLM(service_url=...)`` or, for the
module-level ``uc_handler``, the consumer's ``UC_GROK_SERVICE_URL``. With
none configured, Grok requests fail with an actionable error; there is no
local-browser fallback.

Session resolution (first hit wins):

1. ``extra_body["session_id"]`` — most explicit.
2. ``metadata["session_id"]`` — surfaces in ``litellm_session_id``.

An explicit session id names a persistent service session (shared with the
MCP ``chat`` tool) that continues its conversation across calls. A request
without one runs in a unique service session sent with ``ephemeral=True``
(the service keeps no persistent map entry for it). Once a waited-for reply
is complete the provider forgets that session, which closes the service's
page and map entry only and never deletes the account conversation. A blind
request (``wait_for_response=False``) is not forgotten: its page stays alive
and the service discards the ephemeral session at idle eviction.

``extra_body["conversation_url"]`` is sent to the service as provided; the
service validates it against its configured folder.

Blind / fire-and-forget mode:

Pass ``extra_body={"wait_for_response": False}`` to submit the message
without waiting for Grok to generate a reply. The completion returns as
soon as the URL settles to ``/c/<uuid>``. ``message.content`` will be
empty; the conversation_url is still surfaced on ``_hidden_params`` so
callers can read it later with a separate call. Useful for batching
many prompts when the caller doesn't need to parse each one.

Streaming is real: the service's NDJSON deltas are forwarded as they
render and the final result reconciles any remainder.

Limitations:

* Token counts are length-based estimates (browser-driven; no real
  tokenizer output available).
* A send is never retried or resent: after a failure the message may
  already have been submitted.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import queue as _queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Iterator, Optional

import litellm
from litellm import CustomLLM
from litellm.types.utils import GenericStreamingChunk, ModelResponse, Usage

from uc_browser.service.client import ServiceClient, ServiceError, UCServiceGone

logger = logging.getLogger("uc_browser.llm_providers.uc")

PROVIDER = "uc"
SUPPORTED_MODELS: set[str] = {"grok"}

#: Consumer configuration naming the UC service that hosts the Grok site.
GROK_SERVICE_URL_ENV = "UC_GROK_SERVICE_URL"
GROK_SITE = "grok"

#: Service sessions this package creates for itself. They never appear as
#: user-visible chat sessions (``is_internal_session``).
INTERNAL_SESSION_PREFIX = "_uc-"
#: Shared session for operations that are not a chat (read, list, rename...).
ADMIN_SESSION = "_uc-admin"
_EPHEMERAL_PREFIX = "_uc-tmp-"

_DELTA, _DONE, _FAILED = "delta", "done", "failed"


def ephemeral_session_id() -> str:
    """A service session id no other caller can share or continue."""
    return f"{_EPHEMERAL_PREFIX}{uuid.uuid4().hex}"


def is_internal_session(session_id: str) -> bool:
    return str(session_id).startswith(INTERNAL_SESSION_PREFIX)


def _describe(exc: BaseException) -> str:
    """Content-free summary of a service failure, safe to log."""
    code = getattr(exc, "code", None)
    return f"{type(exc).__name__}: {code}" if code else type(exc).__name__


def forget_service_session(client: ServiceClient, session_id: str) -> None:
    """Drop one service session (its page and map entry). Never deletes the
    account conversation. Best effort: an already-gone session is fine and
    a failure must not mask the caller's own outcome."""
    try:
        client.forget_session(GROK_SITE, session_id)
    except (ServiceError, OSError) as exc:
        if getattr(exc, "code", None) != "no_such_session":
            logger.warning("could not forget service session %s (%s)",
                           session_id, _describe(exc))


# A request that conflicts with the session's state (for example reusing a
# session with a different ephemeral marking) is the caller's to fix.
_BAD_REQUEST_CODES = {"folder_mismatch", "bad_arguments", "session_conflict"}
# The session or its page is not there to serve the request. The send may or
# may not have been submitted (``session_lost``, ``cancelled``): the provider
# never resends, whatever the code.
_UNAVAILABLE_CODES = {
    "busy", "page_deviation", "window_stopped", "no_such_site", "no_such_op",
    "session_lost", "session_closed", "session_unavailable", "cancelled",
}


def _litellm_error(exc: ServiceError, *, model: str) -> Exception:
    """The litellm exception for a service failure. Partial text from a
    timeout is deliberately left out of the message (it is account content
    and exceptions get logged); streaming callers already received it."""
    detail = (exc.detail or "")[:300]
    where = {"model": model, "llm_provider": PROVIDER}
    if exc.code == "auth_required":
        return litellm.exceptions.AuthenticationError(
            message=f"Grok auth required: {detail}", **where)
    if exc.code == "timeout":
        return litellm.exceptions.Timeout(
            message=(
                "UC service: Grok did not finish before the timeout "
                f"({len(exc.detail or '')} chars of partial text rendered; "
                "the message was not resent)"
            ),
            **where)
    if exc.code in _BAD_REQUEST_CODES:
        return litellm.exceptions.BadRequestError(
            message=f"UC service refused the request ({exc.code}): {detail}", **where)
    if isinstance(exc, UCServiceGone):
        return litellm.exceptions.ServiceUnavailableError(
            message=f"UC service unreachable: {detail}", **where)
    if exc.code in _UNAVAILABLE_CODES:
        return litellm.exceptions.ServiceUnavailableError(
            message=f"UC service cannot serve Grok right now ({exc.code}): {detail}",
            **where)
    return litellm.exceptions.APIError(
        status_code=exc.status,
        message=f"UC service error ({exc.code}): {detail}",
        llm_provider=PROVIDER,
        model=model,
    )


@contextlib.contextmanager
def _service_errors(model: str):
    """Translate service failures raised inside the block to litellm's."""
    try:
        yield
    except ServiceError as exc:
        raise _litellm_error(exc, model=model) from exc
    except TimeoutError as exc:
        raise litellm.exceptions.Timeout(
            message="UC service did not answer before the read timeout",
            model=model,
            llm_provider=PROVIDER,
        ) from exc


# ── Helpers ──────────────────────────────────────────────────────────


def _generic_site_entry(site: str):
    """Registry entry for ``site`` when it routes through the generic
    engine: explicitly advertised as uc/<site>, or auto-bootstrapped
    (chat-kind sites are generically drivable by default; widget-kind
    never bootstraps — see registry.advertised_model)."""
    try:
        from uc_browser.registry import advertised_model, get_registry

        entry = get_registry().get(site)
        if entry is not None:
            model, _bootstrapped = advertised_model(entry)
            if model == f"{PROVIDER}/{site}":
                return entry
    except Exception:  # pragma: no cover — registry must never break routing
        logger.debug("generic-site lookup failed for %r", site, exc_info=True)
    return None


def _backup_model_for(site: str, optional_params: dict) -> Optional[str]:
    """Conventional-API fallback for a down/blocked site: the registry
    entry's backup_model, else the UC_BACKUP_MODEL env default. Disabled
    per-request with extra_body={'no_backup': true}."""
    if _from_extras(optional_params, "no_backup"):
        return None
    try:
        from uc_browser.registry import get_registry

        entry = get_registry().get(site)
        if entry and entry.backup_model:
            return entry.backup_model
    except Exception:
        pass
    return os.environ.get("UC_BACKUP_MODEL") or None


def _check_availability(site: str, model: str, optional_params: dict) -> None:
    """Fail fast when the health monitor says the site is down.

    Reads ``data/health/latest.json`` (written by uc_browser.health).
    Graceful by design: no health data, stale data, or an unreadable file
    all mean "no opinion" — the request proceeds. Disable entirely with
    ``UC_AVAILABILITY_GATE=0``, or per-request with
    ``extra_body={"ignore_availability": true}``.
    """
    if os.environ.get("UC_AVAILABILITY_GATE", "1") == "0":
        return
    if _from_extras(optional_params, "ignore_availability"):
        return
    try:
        from uc_browser.health import STATUS_DOWN, STATUS_LOGIN, HealthStore
        from uc_browser.registry import get_registry

        entry = get_registry().get(site)
        rec = HealthStore().latest(site)
        if not entry or not rec:
            return
        age = time.time() - rec.get("at", 0)
        if age > 2 * entry.probe_interval_s:
            return  # stale — no opinion
        if rec.get("status") in (STATUS_DOWN, STATUS_LOGIN):
            raise litellm.exceptions.ServiceUnavailableError(
                message=(
                    f"UCBrowserCustomLLM: site {site!r} is currently "
                    f"{rec['status']} (probed {int(age)}s ago: "
                    f"{rec.get('detail', '')}). Check GET /uc/availability, "
                    "or pass extra_body={'ignore_availability': true} to "
                    "try anyway."
                ),
                model=model,
                llm_provider=PROVIDER,
            )
    except litellm.exceptions.ServiceUnavailableError:
        raise
    except Exception:  # pragma: no cover — the gate must never break sends
        logger.debug("availability gate check failed; allowing request",
                     exc_info=True)


def _last_user_message(messages: list[dict]) -> str:
    """Return the text of the most recent ``role=user`` message."""
    for msg in reversed(messages or []):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # OpenAI multimodal — concatenate text parts.
            texts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "\n".join(t for t in texts if t)
    return ""


def _from_extras(optional_params: dict, key: str):
    """Pull ``key`` from ``optional_params`` regardless of nesting.

    Direct ``litellm.completion(extra_body={...})`` preserves the dict
    nested under ``optional_params["extra_body"]``. The litellm proxy
    flattens ``extra_body`` entries into top-level ``optional_params``
    keys. Check both shapes so the same handler covers both callers.
    """
    op = optional_params or {}
    eb = op.get("extra_body") or {}
    if isinstance(eb, dict) and key in eb:
        return eb[key]
    if key in op:
        return op[key]
    return None


def _resolve_session_key(
    litellm_params: dict | None,
    optional_params: dict,
) -> Optional[str]:
    sid = _from_extras(optional_params, "session_id")
    if sid:
        return str(sid)
    sid = (litellm_params or {}).get("litellm_session_id")
    return str(sid) if sid else None


def _resolve_conversation_url(optional_params: dict) -> Optional[str]:
    url = _from_extras(optional_params, "conversation_url")
    return str(url) if url else None


def _resolve_wait_for_response(optional_params: dict) -> bool:
    v = _from_extras(optional_params, "wait_for_response")
    if v is None:
        return True
    return bool(v)


def _populate_response(
    *,
    model_response: ModelResponse,
    model_name: str,
    content: str,
    conversation_id: Optional[str],
    conversation_url: Optional[str],
    prompt_text: str,
) -> ModelResponse:
    """Fill the pre-allocated ModelResponse litellm hands us."""
    model_response.choices[0].message.content = content
    model_response.choices[0].finish_reason = "stop"
    model_response.model = model_name
    model_response.created = int(time.time())
    # Rough estimate — no tokenizer in play for browser-driven chat.
    pt = max(1, len(prompt_text) // 4)
    ct = max(1, len(content) // 4)
    model_response.usage = Usage(
        prompt_tokens=pt,
        completion_tokens=ct,
        total_tokens=pt + ct,
    )
    hp = getattr(model_response, "_hidden_params", None) or {}
    hp["uc_conversation_id"] = conversation_id
    hp["uc_conversation_url"] = conversation_url
    model_response._hidden_params = hp
    return model_response


# ── CustomLLM ────────────────────────────────────────────────────────


class UCBrowserCustomLLM(CustomLLM):
    """litellm CustomLLM that delegates ``uc/<site>`` requests.

    ``uc/grok`` is a thin client of the UC service's ``/sites/grok`` routes:
    the service owns the browser, the conversation map and the Grok folder
    policy. ``service_url`` (or an injected ``client``) is explicit; with
    neither, Grok requests fail with an actionable error and nothing opens a
    local browser."""

    def __init__(
        self,
        service_url: Optional[str] = None,
        *,
        client: Optional[ServiceClient] = None,
    ) -> None:
        super().__init__()
        url = (service_url or "").strip()
        if client is not None and url:
            raise ValueError("pass service_url or client, not both")
        if client is None and url:
            client = ServiceClient(url)
        self._client = client
        # The generic engine still drives its own browser on a pinned
        # thread: only one sync Playwright instance can start per thread.
        self._generic_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="uc-generic-pw",
        )

    @staticmethod
    def _pin(executor: ThreadPoolExecutor, prefix: str, fn, *args, **kwargs):
        """Run ``fn`` on the given single-worker executor (inline if we're
        already on its thread). Sync Playwright objects are thread-affine,
        and caller threads may carry litellm's leftover asyncio loop, so
        every browser-touching call must hop to its pinned worker — in the
        sync path too, not just the async ones."""
        if threading.current_thread().name.startswith(prefix):
            return fn(*args, **kwargs)
        return executor.submit(fn, *args, **kwargs).result()

    def _pin_to_generic_thread(self, fn, *args, **kwargs):
        return self._pin(self._generic_executor, "uc-generic-pw",
                         fn, *args, **kwargs)

    def _grok_client(self, model: str) -> ServiceClient:
        if self._client is None:
            raise litellm.exceptions.ServiceUnavailableError(
                message=(
                    "UCBrowserCustomLLM: no UC service is configured for Grok. "
                    f"Set {GROK_SERVICE_URL_ENV} to the service's URL (for "
                    "example http://127.0.0.1:4010) before this process "
                    "starts. There is no local-browser fallback."
                ),
                model=model,
                llm_provider=PROVIDER,
            )
        return self._client

    # ── litellm CustomLLM API ────────────────────────────────────────

    def completion(  # type: ignore[override]
        self,
        model: str,
        messages: list,
        model_response: ModelResponse,
        optional_params: dict,
        litellm_params: dict | None = None,
        timeout: Any = None,
        on_delta=None,
        **kwargs: Any,
    ) -> ModelResponse:
        # litellm strips the ``uc/`` prefix before calling us; ``model``
        # here is just the site name (e.g. "grok").
        site = (model or "").strip().lower()
        generic_entry = None
        if site not in SUPPORTED_MODELS:
            # Not adapter-backed — is it a registry site advertised as a
            # generic litellm model? Those route through the generic
            # engine (uc_browser.sites.generic) instead of a hand-tuned
            # driver.
            generic_entry = _generic_site_entry(site)
            if generic_entry is None:
                raise litellm.exceptions.BadRequestError(
                    message=(
                        f"UCBrowserCustomLLM: unsupported model {model!r}. "
                        f"Adapter-backed: {sorted(SUPPORTED_MODELS)}; "
                        "generic sites: registry entries with "
                        f"litellm_model='uc/<name>' (see /uc/availability)."
                    ),
                    model=model,
                    llm_provider=PROVIDER,
                )

        backup = _backup_model_for(site, optional_params)
        if generic_entry is not None:
            # This local health store describes local probe browsers, not the
            # explicitly selected service's signed-in Grok window. Grok's
            # adapter diagnoses its own expected page in the service.
            try:
                _check_availability(site, model, optional_params)
            except litellm.exceptions.ServiceUnavailableError as exc:
                if backup:
                    return self._backup_completion(
                        backup, site, messages, model_response, cause=str(exc))
                raise
            try:
                return self._generic_completion(
                    site=site,
                    entry=generic_entry,
                    messages=messages,
                    model_response=model_response,
                    optional_params=optional_params,
                    litellm_params=litellm_params,
                    timeout=timeout,
                    model=model,
                )
            except litellm.exceptions.ServiceUnavailableError as exc:
                if backup:
                    return self._backup_completion(
                        backup, site, messages, model_response, cause=str(exc))
                raise

        # Note: stream=True doesn't reach this method — litellm routes
        # streaming requests to ``streaming``/``astreaming`` below, which
        # call back here with ``on_delta`` set.

        # Control message: thread cleanup. Reachable via
        # ``extra_body={"action": "cleanup_threads", "keep_recent": N}``.
        # Skips the send entirely and asks the service to delete all but the
        # N most-recent conversations of its configured folder. No user
        # message required.
        if _from_extras(optional_params, "action") == "cleanup_threads":
            keep_raw = _from_extras(optional_params, "keep_recent")
            try:
                keep_recent = max(0, int(keep_raw)) if keep_raw is not None else 1
            except (TypeError, ValueError):
                keep_recent = 1
            summary = self._cleanup_threads(keep_recent, model)
            return _populate_response(
                model_response=model_response,
                model_name=f"{PROVIDER}/{site}",
                content=summary,
                conversation_id=None,
                conversation_url=None,
                prompt_text="",
            )

        prompt_text = _last_user_message(messages)
        if not prompt_text:
            raise litellm.exceptions.BadRequestError(
                message="UCBrowserCustomLLM: no user message found in messages.",
                model=model,
                llm_provider=PROVIDER,
            )

        # Browser renders of large-context prompts can take minutes (a
        # reasoning model on a 50K-char prompt runs ~200s+). Floor the
        # service-side wait at UC_GROK_TIMEOUT_S (default 300) so a
        # slow-but-fine render is not reported as a failed send.
        _floor = int(os.environ.get("UC_GROK_TIMEOUT_S", "300"))
        timeout_s = int(timeout) if isinstance(timeout, (int, float)) and timeout else _floor
        if timeout_s < _floor:
            timeout_s = _floor
        session_key = _resolve_session_key(litellm_params, optional_params)
        explicit_url = _resolve_conversation_url(optional_params)
        wait_for_response = _resolve_wait_for_response(optional_params)

        client = self._grok_client(model)
        # An explicit session id names a persistent service session (shared
        # with the MCP ``chat`` tool) that continues its conversation. A
        # request without one gets a unique session sent with
        # ``ephemeral=True`` (the service never persists its map entry), so
        # it can never continue someone else's chat.
        ephemeral = session_key is None
        session_id = ephemeral_session_id() if ephemeral else session_key
        send_kwargs: dict[str, Any] = {
            "message": prompt_text,
            "timeout_s": timeout_s,
            "wait_for_response": wait_for_response,
        }
        if explicit_url is not None:
            # Sent as provided: the service validates it against its folder.
            send_kwargs["conversation_url"] = explicit_url

        try:
            try:
                with _service_errors(model):
                    result = self._service_send(
                        client, send_kwargs, session_id, on_delta, ephemeral)
            except litellm.exceptions.AuthenticationError as exc:
                if backup:
                    return self._backup_completion(
                        backup, site, messages, model_response,
                        cause=f"auth required: {exc}")
                raise

            # Delete-after-capture (opt-in via UC_GROK_DELETE_AFTER_CAPTURE=1).
            # The reply prose is already in ``result``, so once captured the
            # thread is disposable. OFF by default so threads persist for
            # review. Only delete on a NON-EMPTY capture — an empty/failed
            # render keeps its thread for diagnosis. Best-effort; failures
            # never block the response. The service enforces its folder
            # policy on the delete.
            captured = (result.get("response") or "").strip()
            if (
                os.environ.get("UC_GROK_DELETE_AFTER_CAPTURE") == "1"
                and result.get("url")
                and captured
            ):
                self._delete_captured(client, result["url"], session_id, ephemeral)
        finally:
            # A completed (waited-for) ephemeral request closes its own page
            # and map entry. A blind one (wait_for_response=False) returns as
            # soon as the message is submitted, so closing the page then would
            # cut off a generation we never watched: the service-owned page
            # stays alive and the service discards the ephemeral session when
            # it is evicted for idleness.
            if ephemeral and wait_for_response:
                forget_service_session(client, session_id)

        return _populate_response(
            model_response=model_response,
            model_name=f"{PROVIDER}/{site}",
            content=result.get("response") or "",
            conversation_id=result.get("conversation_id"),
            conversation_url=result.get("url"),
            prompt_text=prompt_text,
        )

    @staticmethod
    def _service_send(
        client: ServiceClient,
        send_kwargs: dict,
        session_id: str,
        on_delta,
        ephemeral: bool = False,
    ) -> dict:
        """One ``send`` request. With ``on_delta`` the route is streamed and
        each delta is forwarded as it arrives; the result line is the
        authoritative reply. Never retried: an uncertain send is not resent."""
        if on_delta is None:
            result = client.site(
                GROK_SITE, "send", send_kwargs, session_id=session_id, ephemeral=ephemeral,
            )
        else:
            result = None
            with client.site(
                GROK_SITE, "send", send_kwargs, session_id=session_id,
                stream=True, ephemeral=ephemeral,
            ) as stream:
                for line in stream:
                    if "delta" in line:
                        on_delta(line["delta"])
                    elif "result" in line:
                        result = line["result"]
                        break
            if result is None:
                raise ServiceError(
                    "stream_truncated",
                    "the service stream ended without a result; the message "
                    "may already have been submitted and was not resent",
                    502,
                )
        if not isinstance(result, dict):
            raise ServiceError(
                "bad_response", "the send route returned a non-object result", 502,
            )
        return result

    @staticmethod
    def _delete_captured(
        client: ServiceClient, url: str, session_id: str, ephemeral: bool,
    ) -> None:
        """Best-effort delete of the one conversation this request created,
        through the service (which enforces the folder). A persistent session
        whose conversation is gone is forgotten so its next call starts
        fresh instead of continuing a deleted thread."""
        try:
            deleted = client.site(
                GROK_SITE, "delete", {"conversation_url": url},
                session_id=session_id, ephemeral=ephemeral,
            )
        except (ServiceError, OSError) as exc:
            logger.warning("[uc] delete-after-capture failed (%s)", _describe(exc))
            return
        logger.info("[uc] delete-after-capture %s",
                    "deleted" if deleted else "row-not-found")
        if deleted and not ephemeral:
            forget_service_session(client, session_id)

    def _generic_completion(
        self,
        *,
        site: str,
        entry,
        messages: list,
        model_response: ModelResponse,
        optional_params: dict,
        litellm_params: dict | None,
        timeout: Any,
        model: str,
    ) -> ModelResponse:
        """Route a registry site through the generic engine (no adapter)."""
        from uc_browser.sites.generic import GenericSiteError, get_generic_client

        prompt_text = _last_user_message(messages)
        if not prompt_text:
            raise litellm.exceptions.BadRequestError(
                message="UCBrowserCustomLLM: no user message found in messages.",
                model=model,
                llm_provider=PROVIDER,
            )
        timeout_s = int(timeout) if isinstance(timeout, (int, float)) and timeout else 60
        session_key = _resolve_session_key(litellm_params, optional_params)
        try:
            result = self._pin_to_generic_thread(
                get_generic_client().send,
                site, entry.url, prompt_text,
                session_key=session_key, timeout_s=timeout_s,
            )
        except GenericSiteError as exc:
            raise litellm.exceptions.ServiceUnavailableError(
                message=f"uc/{site} (generic engine): {exc}",
                model=model,
                llm_provider=PROVIDER,
            ) from exc
        return _populate_response(
            model_response=model_response,
            model_name=f"{PROVIDER}/{site}",
            content=result["response"],
            conversation_id=None,
            conversation_url=result.get("page_url"),
            prompt_text=prompt_text,
        )

    def _backup_completion(
        self,
        backup_model: str,
        site: str,
        messages: list,
        model_response: ModelResponse,
        *,
        cause: str,
    ) -> ModelResponse:
        """Route the request to a conventional-API litellm model because
        the browser-driven site is down/blocked. The response is labeled
        via hidden params so callers can tell a backup answer from a
        site-rendered one."""
        logger.warning("uc/%s unavailable (%s) — falling back to %s",
                       site, cause[:120], backup_model)
        resp = litellm.completion(model=backup_model, messages=messages)
        content = resp.choices[0].message.content or ""
        out = _populate_response(
            model_response=model_response,
            model_name=f"{PROVIDER}/{site}",
            content=content,
            conversation_id=None,
            conversation_url=None,
            prompt_text=_last_user_message(messages),
        )
        hp = getattr(out, "_hidden_params", None) or {}
        hp["uc_backup_used"] = backup_model
        hp["uc_backup_reason"] = cause[:200]
        out._hidden_params = hp
        return out

    def _cleanup_threads(self, keep_recent: int, model: str) -> str:
        """Ask the service to delete all but the ``keep_recent`` most recent
        conversations of the configured Grok folder.

        The service owns the folder policy and the deletion; this client
        sends one request and never enumerates or deletes conversations
        itself. The service answers ``{summary: str, deleted_ids: [...]}``
        (the ids let it invalidate its own persisted mappings); the external
        result stays the summary text."""
        client = self._grok_client(model)
        with _service_errors(model):
            result = client.site(
                GROK_SITE, "cleanup_threads", {"keep_recent": keep_recent},
                session_id=ADMIN_SESSION,
            )
            if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
                raise ServiceError(
                    "bad_response",
                    "cleanup_threads returned no summary text",
                    502,
                )
        return result["summary"]

    async def acompletion(  # type: ignore[override]
        self,
        model: str,
        messages: list,
        model_response: ModelResponse,
        optional_params: dict,
        litellm_params: dict | None = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> ModelResponse:
        # The service call is blocking HTTP: keep it off the event loop.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            functools.partial(
                self.completion,
                model=model,
                messages=messages,
                model_response=model_response,
                optional_params=optional_params,
                litellm_params=litellm_params,
                timeout=timeout,
                **kwargs,
            ),
        )

    # ── Streaming (real) ─────────────────────────────────────────────
    #
    # The service streams a Grok send as NDJSON ``{"delta"}`` lines and one
    # final ``{"result"}``. ``completion(on_delta=...)`` reads that stream on
    # a worker thread and pushes each delta onto a queue; the (a)streaming
    # generator drains the queue and yields chunks LIVE. A final
    # reconciliation emits any text of the result the deltas did not carry
    # (the generic engine, the backup model and cleanup do not stream), then
    # the terminal usage chunk. An abandoned consumer does not cancel the
    # send: it was already submitted, and the worker runs it to completion.

    @staticmethod
    def _content_chunk(text: str) -> GenericStreamingChunk:
        return GenericStreamingChunk(
            text=text, tool_use=None, is_finished=False,
            finish_reason="", usage=None, index=0,
        )

    @staticmethod
    def _final_chunk(usage_block) -> GenericStreamingChunk:
        return GenericStreamingChunk(
            text="", tool_use=None, is_finished=True,
            finish_reason="stop", usage=usage_block, index=0,
        )

    @staticmethod
    def _usage_block(response: ModelResponse):
        if getattr(response, "usage", None) is None:
            return None
        try:
            return {
                "prompt_tokens": int(response.usage.prompt_tokens or 0),
                "completion_tokens": int(response.usage.completion_tokens or 0),
                "total_tokens": int(response.usage.total_tokens or 0),
            }
        except Exception:  # noqa: BLE001
            return None

    def _reconcile_tail(self, response: ModelResponse, streamed: str):
        """Yield any final content not already streamed (fallback paths)."""
        try:
            final = response.choices[0].message.content or ""
        except Exception:  # noqa: BLE001
            final = ""
        if final and final.startswith(streamed) and len(final) > len(streamed):
            yield self._content_chunk(final[len(streamed):])
        elif final and not streamed:
            yield self._content_chunk(final)

    def streaming(  # type: ignore[override]
        self,
        model: str,
        messages: list,
        model_response: ModelResponse,
        optional_params: dict,
        litellm_params: dict | None = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> Iterator[GenericStreamingChunk]:
        q: "_queue.Queue" = _queue.Queue()

        def _on_delta(text: str) -> None:
            if text:
                q.put((_DELTA, text))

        def _run() -> None:
            try:
                response = self.completion(
                    model=model, messages=messages, model_response=model_response,
                    optional_params=optional_params, litellm_params=litellm_params,
                    timeout=timeout, on_delta=_on_delta, **kwargs,
                )
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                q.put((_FAILED, exc))
            else:
                q.put((_DONE, response))

        threading.Thread(target=_run, name="uc-grok-stream", daemon=True).start()

        streamed = ""
        while True:
            kind, payload = q.get()
            if kind == _DELTA:
                streamed += payload
                yield self._content_chunk(payload)
            elif kind == _FAILED:
                raise payload
            else:
                response = payload
                break
        yield from self._reconcile_tail(response, streamed)
        yield self._final_chunk(self._usage_block(response))

    async def astreaming(  # type: ignore[override]
        self,
        model: str,
        messages: list,
        model_response: ModelResponse,
        optional_params: dict,
        litellm_params: dict | None = None,
        timeout: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[GenericStreamingChunk]:
        loop = asyncio.get_running_loop()
        aq: "asyncio.Queue" = asyncio.Queue()

        def _on_delta(text: str) -> None:
            if text:
                loop.call_soon_threadsafe(aq.put_nowait, (_DELTA, text))

        fut = loop.run_in_executor(
            None,
            functools.partial(
                self.completion,
                model=model, messages=messages, model_response=model_response,
                optional_params=optional_params, litellm_params=litellm_params,
                timeout=timeout, on_delta=_on_delta, **kwargs,
            ),
        )
        # Deltas are queued with call_soon_threadsafe before the worker
        # returns, so this sentinel always lands after the last of them.
        fut.add_done_callback(lambda _f: aq.put_nowait((_DONE, None)))

        streamed = ""
        while True:
            kind, text = await aq.get()
            if kind == _DONE:
                break
            streamed += text
            yield self._content_chunk(text)
        response = await fut  # propagate the final response / any exception
        for chunk in self._reconcile_tail(response, streamed):
            yield chunk
        yield self._final_chunk(self._usage_block(response))


# ── Registration ─────────────────────────────────────────────────────


# Module-level singleton — eagerly instantiated so the litellm proxy can
# import it by dotted path (``uc_browser.llm_providers.uc.uc_handler``)
# in its ``custom_provider_map`` config. Construction opens no connection;
# the service address is the consumer's explicit ``UC_GROK_SERVICE_URL``
# and, when it is unset, Grok requests fail with an actionable error
# instead of falling back to a local browser.
uc_handler: "UCBrowserCustomLLM" = UCBrowserCustomLLM(
    service_url=os.environ.get(GROK_SERVICE_URL_ENV),
)

_registered_handler: UCBrowserCustomLLM | None = None
_register_lock = threading.Lock()


def register_uc_provider() -> UCBrowserCustomLLM:
    """Attach :data:`uc_handler` to ``litellm.custom_provider_map``.

    Idempotent — re-calling is a no-op. Direct-Python callers use this
    to enable ``litellm.completion(model="uc/grok", ...)``. The litellm
    proxy wires the same handler via its config file's
    ``custom_provider_map`` and doesn't need this call.
    """
    global _registered_handler
    with _register_lock:
        if _registered_handler is not None:
            return _registered_handler
        existing = list(litellm.custom_provider_map or [])
        # Drop any prior entry for the same provider key — keeps the
        # map well-formed if some other code wired its own handler.
        existing = [e for e in existing if e.get("provider") != PROVIDER]
        existing.append({"provider": PROVIDER, "custom_handler": uc_handler})
        litellm.custom_provider_map = existing
        _registered_handler = uc_handler
        logger.info("Registered UCBrowserCustomLLM under provider %r.", PROVIDER)
        return uc_handler


def get_uc_handler() -> UCBrowserCustomLLM | None:
    """Return the registered handler, or ``None`` if not yet registered."""
    return _registered_handler


def _reset_for_tests() -> None:
    """Clear the registration. Test-only — needed because the handler is a
    module-level singleton."""
    global _registered_handler
    with _register_lock:
        _registered_handler = None
        litellm.custom_provider_map = [
            e for e in (litellm.custom_provider_map or [])
            if e.get("provider") != PROVIDER
        ]
