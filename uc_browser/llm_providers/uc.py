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
        **kwargs: Any,
    ) -> ModelResponse:
        # litellm strips the ``uc/`` prefix before calling us; ``model``
        # here is just the site name (e.g. "grok").
        site = (model or "").strip().lower()
        if site not in SUPPORTED_MODELS:
            raise litellm.exceptions.BadRequestError(
                message=(
                    f"UCBrowserCustomLLM: unsupported model {model!r}. "
                    f"Supported: {sorted(SUPPORTED_MODELS)}."
                ),
                model=model,
                llm_provider=PROVIDER,
            )

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
            result = _grok_send(
                prompt_text,
                conversation_url=conversation_url,
                timeout_s=timeout_s,
                wait_for_response=wait_for_response,
                session_key=session_key,
            )
        except GrokAuthRequired as exc:
            raise litellm.exceptions.AuthenticationError(
                message=f"Grok auth required: {exc}",
                model=model,
                llm_provider=PROVIDER,
            ) from exc

        if session_key is not None and result.get("url"):
            self.set_session_url(session_key, result["url"])

        return _populate_response(
            model_response=model_response,
            model_name=f"{PROVIDER}/{site}",
            content=result.get("response") or "",
            conversation_id=result.get("conversation_id"),
            conversation_url=result.get("url"),
            prompt_text=prompt_text,
        )

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

    # ── Streaming (fake) ─────────────────────────────────────────────
    #
    # GrokClient.send() is anchor-locked / blocking — it doesn't expose
    # token-level streaming. To stay compatible with clients that require
    # ``stream=True`` (Cline, Open WebUI, some Cursor flows) we emit the
    # full response as a single SSE content chunk followed by a final
    # ``is_finished=True`` chunk. This is the "fake streaming" pattern.

    def _stream_chunks(self, response: ModelResponse) -> Iterator[GenericStreamingChunk]:
        content = ""
        try:
            content = response.choices[0].message.content or ""
        except Exception:
            content = ""
        usage_block = None
        if getattr(response, "usage", None) is not None:
            try:
                usage_block = {
                    "prompt_tokens": int(response.usage.prompt_tokens or 0),
                    "completion_tokens": int(response.usage.completion_tokens or 0),
                    "total_tokens": int(response.usage.total_tokens or 0),
                }
            except Exception:
                usage_block = None
        yield GenericStreamingChunk(
            text=content,
            tool_use=None,
            is_finished=False,
            finish_reason="",
            usage=None,
            index=0,
        )
        yield GenericStreamingChunk(
            text="",
            tool_use=None,
            is_finished=True,
            finish_reason="stop",
            usage=usage_block,
            index=0,
        )

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
        response = self.completion(
            model=model,
            messages=messages,
            model_response=model_response,
            optional_params=optional_params,
            litellm_params=litellm_params,
            timeout=timeout,
            **kwargs,
        )
        yield from self._stream_chunks(response)

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
        response = await self.acompletion(
            model=model,
            messages=messages,
            model_response=model_response,
            optional_params=optional_params,
            litellm_params=litellm_params,
            timeout=timeout,
            **kwargs,
        )
        for chunk in self._stream_chunks(response):
            yield chunk


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
