"""litellm CustomLLM that drives browser-automated chats (Grok today).

Register once with :func:`register_uc_provider`, then call litellm with
``model="uc/grok"``. The handler holds a session store mapping a
caller-supplied id to a Grok conversation URL so subsequent calls
continue the same chat instead of starting a fresh one.

Session key resolution priority (first hit wins):

1. ``extra_body["session_id"]`` — most explicit.
2. ``extra_body["conversation_url"]`` — bypass the store entirely.
3. ``metadata["session_id"]`` — surfaces in ``litellm_session_id``.

If none of those are passed, every call creates a new conversation.

Blind / fire-and-forget mode:

Pass ``extra_body={"wait_for_response": False}`` to submit the message
without waiting for Grok to generate a reply. The completion returns as
soon as the URL settles to ``/c/<uuid>``. ``message.content`` will be
empty; the conversation_url is still surfaced on ``_hidden_params`` so
callers can read it later with a separate call. Useful for batching
many prompts when the caller doesn't need to parse each one.

Limitations:

* No streaming — ``stream=True`` raises ``BadRequestError``.
* Token counts are length-based estimates (browser-driven; no real
  tokenizer output available).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import queue as _queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Iterator, Optional

import litellm
from litellm import CustomLLM
from litellm.types.utils import GenericStreamingChunk, ModelResponse, Usage

from uc_browser.sites.grok import GrokAuthRequired
from uc_browser.sites.grok_fast import send_with_fallback as _grok_send

logger = logging.getLogger("uc_browser.llm_providers.uc")

PROVIDER = "uc"
SUPPORTED_MODELS: set[str] = {"grok"}


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
    """litellm CustomLLM that delegates ``uc/<site>`` to a site client."""

    def __init__(self) -> None:
        super().__init__()
        self._sessions: dict[str, str] = {}
        self._sessions_lock = threading.Lock()
        # Pin Playwright's greenlet to a single thread. asyncio.to_thread
        # uses the default ThreadPoolExecutor (~min(32, cpu+4) workers), so
        # consecutive acompletion calls land on different pool workers and
        # the second one trips "Cannot switch to a different thread" on
        # the sync Playwright greenlet. A 1-worker executor pins every
        # browser-touching call to the same thread for the process
        # lifetime; UCBrowser lazy-inits on first call inside this worker
        # and stays bound to it.
        self._browser_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="uc-grok-pw",
        )
        # The generic engine gets its OWN pinned thread: only one sync
        # Playwright instance can ever start per thread (the first one's
        # event loop stays bound to it and the second start() dies with
        # "sync API inside asyncio loop"), and GrokClient + GenericClient
        # are two separate browser instances by design.
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

    def _pin_to_browser_thread(self, fn, *args, **kwargs):
        return self._pin(self._browser_executor, "uc-grok-pw",
                         fn, *args, **kwargs)

    def _pin_to_generic_thread(self, fn, *args, **kwargs):
        return self._pin(self._generic_executor, "uc-generic-pw",
                         fn, *args, **kwargs)

    # ── Session store (public for inspection / tests) ────────────────

    def get_session_url(self, session_key: str) -> Optional[str]:
        with self._sessions_lock:
            return self._sessions.get(session_key)

    def set_session_url(self, session_key: str, url: str) -> None:
        with self._sessions_lock:
            self._sessions[session_key] = url

    def forget_session(self, session_key: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(session_key, None)

    def clear_sessions(self) -> None:
        with self._sessions_lock:
            self._sessions.clear()

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
        try:
            _check_availability(site, model, optional_params)
        except litellm.exceptions.ServiceUnavailableError as exc:
            if backup:
                return self._backup_completion(
                    backup, site, messages, model_response, cause=str(exc))
            raise

        if generic_entry is not None:
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
        # streaming requests to ``streaming``/``astreaming`` below.

        # Control message: thread cleanup. Reachable via
        # ``extra_body={"action": "cleanup_threads", "keep_recent": N}``.
        # Skips the browser send entirely — enumerates the sidebar and
        # deletes all but the N most-recent conversations. Runs on THIS
        # thread (the browser-pinned executor worker in proxy mode), so it
        # shares the same GrokClient singleton + browser greenlet as renders.
        # No user message required.
        if _from_extras(optional_params, "action") == "cleanup_threads":
            keep_raw = _from_extras(optional_params, "keep_recent")
            try:
                keep_recent = max(0, int(keep_raw)) if keep_raw is not None else 1
            except (TypeError, ValueError):
                keep_recent = 1
            summary = self._cleanup_threads(keep_recent)
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
        # reasoning model on a 50K-char prompt runs ~200s+). A 60s floor
        # made the fast path's response-wait time out spuriously, which
        # tripped send_with_fallback into re-typing the message (the
        # double-insert). Floor at UC_GROK_TIMEOUT_S (default 300) so a
        # slow-but-fine render isn't mistaken for a failed send.
        _floor = int(os.environ.get("UC_GROK_TIMEOUT_S", "300"))
        timeout_s = int(timeout) if isinstance(timeout, (int, float)) and timeout else _floor
        if timeout_s < _floor:
            timeout_s = _floor
        session_key = _resolve_session_key(litellm_params, optional_params)
        explicit_url = _resolve_conversation_url(optional_params)
        wait_for_response = _resolve_wait_for_response(optional_params)

        conversation_url = explicit_url
        if conversation_url is None and session_key is not None:
            conversation_url = self.get_session_url(session_key)

        try:
            # Tries the React-onSubmit + expect_response fast path first
            # (~1.8s warm continue, ~6.3s cold) and falls back to the
            # DOM-driven GrokClient.send on any RuntimeError. Blind-mode
            # callers (wait_for_response=False) skip the fast path
            # entirely — the DOM path is already optimal there (~1.3s).
            # Reuse session_id as the page bucket so distinct sessions
            # get their own tabs.
            result = self._pin_to_browser_thread(
                _grok_send,
                prompt_text,
                conversation_url=conversation_url,
                timeout_s=timeout_s,
                wait_for_response=wait_for_response,
                session_key=session_key,
                on_delta=on_delta,
            )
        except GrokAuthRequired as exc:
            if backup:
                return self._backup_completion(
                    backup, site, messages, model_response,
                    cause=f"auth required: {exc}")
            raise litellm.exceptions.AuthenticationError(
                message=f"Grok auth required: {exc}",
                model=model,
                llm_provider=PROVIDER,
            ) from exc

        # Delete-after-capture (opt-in via UC_GROK_DELETE_AFTER_CAPTURE=1).
        # The reply prose is already in ``result`` and goes into the response
        # below, so once captured the grok thread is disposable. OFF by default
        # so threads persist for review; flip the env flag on for clean runs.
        # Only delete on a NON-EMPTY capture — an empty/failed render keeps its
        # thread for diagnosis. Best-effort; failures never block the response.
        captured = (result.get("response") or "").strip()
        if (
            os.environ.get("UC_GROK_DELETE_AFTER_CAPTURE") == "1"
            and result.get("url")
            and captured
        ):
            from uc_browser.sites.grok import get_grok_client
            try:
                ok = get_grok_client().delete(result["url"])
                logger.info(
                    "[uc] delete-after-capture %s: %s",
                    "deleted" if ok else "row-not-found", result["url"],
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[uc] delete-after-capture failed (%s): %s", result["url"], exc
                )
            # Thread is gone — don't cache its URL as a continuable session.
        elif session_key is not None and result.get("url"):
            self.set_session_url(session_key, result["url"])

        return _populate_response(
            model_response=model_response,
            model_name=f"{PROVIDER}/{site}",
            content=result.get("response") or "",
            conversation_id=result.get("conversation_id"),
            conversation_url=result.get("url"),
            prompt_text=prompt_text,
        )

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

    def _cleanup_threads(self, keep_recent: int) -> str:
        """Delete all but the ``keep_recent`` most-recent Grok conversations.

        Runs synchronously on the caller's thread. In proxy mode that's the
        single browser-pinned executor worker (see ``__init__``), so it
        reuses the same ``GrokClient`` singleton + browser greenlet as
        renders — no second browser, no thread-affinity violation.

        Grok's sidebar is newest-first, and ``list_conversations`` scrapes in
        DOM order, so ``convs[keep_recent:]`` is the set of older rows to
        cull. Also drops any matching cached session URLs so a later call
        with that session_id starts a fresh conversation instead of trying
        to continue a deleted one.
        """
        from uc_browser.sites.grok import get_grok_client

        client = get_grok_client()
        try:
            convs = client.list_conversations()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cleanup_threads: list_conversations failed: %s", exc)
            return f"cleanup_threads: list failed ({type(exc).__name__})"

        if not convs:
            return "cleanup_threads: no conversations found"

        keep = min(keep_recent, len(convs))
        to_delete = convs[keep:]
        deleted = failed = 0
        deleted_urls: set[str] = set()
        for conv in to_delete:
            url = conv.get("url")
            if not url:
                continue
            try:
                if client.delete(url):
                    deleted += 1
                    deleted_urls.add(url)
                else:
                    failed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("cleanup_threads: delete failed for %s: %s", url, exc)
                failed += 1

        # Evict any cached session URLs that pointed at now-deleted threads.
        if deleted_urls:
            with self._sessions_lock:
                stale = [k for k, v in self._sessions.items() if v in deleted_urls]
                for k in stale:
                    self._sessions.pop(k, None)

        logger.info(
            "cleanup_threads: kept %d, deleted %d, failed %d (total seen %d)",
            keep, deleted, failed, len(convs),
        )
        return (
            f"cleanup_threads: kept {keep}, deleted {deleted}, "
            f"failed {failed}, total_seen {len(convs)}"
        )

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
        # GrokClient uses Playwright's sync API — run it off the event
        # loop. MUST use self._browser_executor (1 worker) and not
        # asyncio.to_thread: Playwright sync is greenlet-thread-bound and
        # the default pool would dispatch consecutive calls to different
        # workers, producing intermittent
        # ``greenlet.error: cannot switch to a different thread``.
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._browser_executor,
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
    # GrokClient has no token API, but the DOM-poll capture
    # (``grok_fast._toolkit_capture`` / ``_poll_dom_response``) already reads
    # the assistant block as it renders. We pass it an ``on_delta`` sink that
    # pushes each incremental chunk onto a thread-safe queue; the (a)streaming
    # generator drains the queue and yields SSE chunks LIVE — so time-to-first-
    # token drops from the full render time (~24s) to grok's first-token
    # latency (~1-3s). The blocking ``completion`` runs on the single browser
    # executor (pushing deltas); the queue drains on a different thread, so
    # there is no greenlet re-entry and no deadlock. A final reconciliation
    # emits any remainder for non-streaming fallback paths (one-shot network
    # capture / ``GrokClient.send``), then the terminal usage chunk.

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
        sentinel = object()

        def _on_delta(text: str) -> None:
            if text:
                q.put(text)

        fut = self._browser_executor.submit(
            functools.partial(
                self.completion,
                model=model, messages=messages, model_response=model_response,
                optional_params=optional_params, litellm_params=litellm_params,
                timeout=timeout, on_delta=_on_delta, **kwargs,
            )
        )
        fut.add_done_callback(lambda _f: q.put(sentinel))

        streamed = ""
        while True:
            item = q.get()
            if item is sentinel:
                break
            streamed += item
            yield self._content_chunk(item)
        response = fut.result()  # propagate the final response / any exception
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
        q: "_queue.Queue" = _queue.Queue()
        sentinel = object()

        def _on_delta(text: str) -> None:
            if text:
                q.put(text)

        fut = loop.run_in_executor(
            self._browser_executor,
            functools.partial(
                self.completion,
                model=model, messages=messages, model_response=model_response,
                optional_params=optional_params, litellm_params=litellm_params,
                timeout=timeout, on_delta=_on_delta, **kwargs,
            ),
        )
        fut.add_done_callback(lambda _f: q.put(sentinel))

        streamed = ""
        while True:
            # Block for the next chunk on a default-pool thread so the event
            # loop stays free (the browser executor is busy with completion).
            item = await loop.run_in_executor(None, q.get)
            if item is sentinel:
                break
            streamed += item
            yield self._content_chunk(item)
        response = await fut  # propagate the final response / any exception
        for chunk in self._reconcile_tail(response, streamed):
            yield chunk
        yield self._final_chunk(self._usage_block(response))


# ── Registration ─────────────────────────────────────────────────────


# Module-level singleton — eagerly instantiated so the litellm proxy can
# import it by dotted path (``uc_browser.llm_providers.uc.uc_handler``)
# in its ``custom_provider_map`` config. Construction has no side effects;
# the underlying browser doesn't open until the first completion call.
uc_handler: "UCBrowserCustomLLM" = UCBrowserCustomLLM()

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
    """Clear the registration + drop cached sessions on the shared
    ``uc_handler``. Test-only — needed because the handler is a
    module-level singleton."""
    global _registered_handler
    with _register_lock:
        _registered_handler = None
        litellm.custom_provider_map = [
            e for e in (litellm.custom_provider_map or [])
            if e.get("provider") != PROVIDER
        ]
    uc_handler.clear_sessions()
