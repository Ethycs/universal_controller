"""Grok via the API-shim path (in-page fetch through ``__UC_apiShim``).

STATUS — NOT WIRED INTO uc/grok YET. Live testing showed Grok's API
gateway rejects calls from the shim with HTTP 403 and
``"Request rejected by anti-bot rules"``. The reason: Grok's UI adds a
rotating ``x-statsig-id`` header (and ``x-xai-request-id``,
``traceparent``, ``sentry-trace``, ``baggage``) inside its
``apiClient.post`` wrapper *before* calling ``window.fetch``. Plain
``window.fetch`` calls from our shim don't go through that wrapper, so
the statsig signature is missing and the request fails the anti-bot
check.

This module is kept as the *shape* of the shim-path client. To make it
work for Grok we'd need to bind to Grok's apiClient wrapper directly
(React-fiber walk from a real React node, or a window-scope scan for
the bundled api client). For sites without statsig-style request
signing (likely ChatGPT, Copilot, Claude.ai), the shim should work
unchanged.

Use ``GrokClient.send`` (DOM-driven, in :mod:`grok`) for now; it's
already at ~2.4s warm. This module stays as the template for future
sites and as the fast-path target when the Grok-specific bind lands.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from uc_browser.sites.grok import (
    GrokAuthRequired,
    GrokClient,
    _conv_id_from_url,
    _strip_thought_prefix,
    get_grok_client,
)

logger = logging.getLogger("uc_browser.sites.grok_api")


# ── Endpoints + request bodies (verified live 2026-05-29) ──────────


GROK_NEW_CHAT_URL = "https://grok.com/rest/app-chat/conversations/new"
GROK_LIST_CHATS_URL = "https://grok.com/rest/app-chat/conversations?pageSize=60"
# Used to fetch the response-node tree for an existing chat. Format with
# the conversation_id.
GROK_RESPONSE_NODE_URL = (
    "https://grok.com/rest/app-chat/conversations/{conv_id}/response-node"
)


def _default_new_chat_body(message: str) -> dict[str, Any]:
    """Build the JSON body Grok's UI sends to .../conversations/new.

    Verified against a live capture. Many of these flags are UI-state
    we don't care about — we send what Grok expects so the request
    validates.
    """
    return {
        "temporary": False,
        "message": message,
        "fileAttachments": [],
        "imageAttachments": [],
        "disableSearch": False,
        "enableImageGeneration": True,
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "enableImageStreaming": True,
        "imageGenerationCount": 2,
        "forceConcise": False,
        "enableSideBySide": True,
        "sendFinalMetadata": True,
        "disableTextFollowUps": False,
        "responseMetadata": {},
        "disableMemory": False,
        "forceSideBySide": False,
        "isAsyncChat": False,
        "disableSelfHarmShortCircuit": False,
        "collectionIds": [],
        "disabledConnectorIds": [],
    }


# ── Response parsing ───────────────────────────────────────────────


def _iter_json_lines(text: str):
    """Yield parsed JSON objects from a newline-delimited stream.

    Tolerates blank lines and partial trailing buffers (yields what
    parses, skips what doesn't).
    """
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            # Some lines might be fragments under aggressive streaming;
            # caller can re-parse if needed.
            continue


def _extract_assistant_text(body: str) -> str:
    """Pull the final assistant text out of Grok's streaming JSON-lines body.

    Two response shapes seen in the wild:
        * NEW chat (``POST /conversations/new``)::
              {"result": {"response": {"modelResponse": {"message": "..."}}}}
        * CONTINUE (``POST /conversations/<uuid>/responses``)::
              {"result": {"token": "..."}}          ← per-token deltas
              {"result": {"modelResponse": {"message": "..."}}}  ← final

    We walk every line, accumulate any token deltas, and replace ``final``
    with any ``modelResponse.message`` we see. The last non-empty
    ``message`` wins.
    """
    final = ""
    delta_buf = ""
    for obj in _iter_json_lines(body):
        result = obj.get("result") or {}
        # NEW-chat shape: result.response.{modelResponse|token}
        resp = result.get("response") or {}
        # Final assembled message — try both shapes.
        for container in (resp, result):
            mr = container.get("modelResponse") or {}
            msg = mr.get("message")
            if msg:
                final = msg
                break
        # Per-token delta — also try both shapes.
        token = resp.get("token") or result.get("token")
        if token:
            delta_buf += token
    out = final or delta_buf
    return _strip_thought_prefix(out)


def _extract_conversation_id(body: str) -> Optional[str]:
    """Find the conversationId in the streaming response."""
    for obj in _iter_json_lines(body):
        result = obj.get("result") or {}
        conv = result.get("conversation") or {}
        cid = conv.get("conversationId")
        if cid:
            return cid
    return None


# ── Client ─────────────────────────────────────────────────────────


class GrokAPIClient:
    """Shim-path client. Reuses GrokClient as the auth oracle / tab owner."""

    def __init__(self, grok_client: Optional[GrokClient] = None) -> None:
        # Reuse the process-singleton GrokClient if not explicitly given,
        # so we share the same Playwright context + per-session tabs as
        # the DOM-driven path.
        self._client = grok_client or get_grok_client()

    # ── send: POST to .../conversations/new from inside the page ────

    def send(
        self,
        message: str,
        *,
        conversation_url: Optional[str] = None,
        session_key: Optional[str] = None,
        timeout_ms: int = 120_000,
    ) -> dict:
        """Send a message via the shim. Returns ``{response, url, conversation_id}``.

        ``conversation_url`` semantics:
            * None or grok.com root  → POST to /conversations/new (creates a chat)
            * /c/<uuid>              → not yet supported here (probe pending);
              callers should fall back to GrokClient.send for now.

        Raises ``RuntimeError`` if the shim returns a non-2xx status — the
        caller is expected to catch and fall back to the DOM path.
        """
        if conversation_url and "/c/" in conversation_url:
            # Continue-chat endpoint not yet captured; let the caller
            # fall back to the DOM path. Raising rather than silently
            # falling back keeps the API path's error semantics clean.
            raise NotImplementedError(
                "GrokAPIClient: continuing an existing chat needs the "
                "/responses endpoint capture — fall back to GrokClient.send."
            )

        body = _default_new_chat_body(message)
        sk = session_key or self._client.DEFAULT_SESSION
        with self._client._session(sk) as page:
            # Make sure the page has grok.com loaded so the shim's
            # ``credentials: 'include'`` picks up the right cookies.
            self._client._navigate_in(page, self._client.GROK_HOME, wait_ms=0)
            result = page.evaluate(
                """(args) => window.__UC_apiShim.fetch(args[0], {
                    method: 'POST',
                    body: args[1],
                })""",
                [GROK_NEW_CHAT_URL, body],
            )
        if not result or not result.get("ok"):
            raise RuntimeError(
                f"GrokAPIClient: shim returned status="
                f"{result.get('status') if result else 'none'}; "
                f"body head={(result.get('body') or '')[:200] if result else 'none'}"
            )
        raw_body = result.get("body") or ""
        text = _extract_assistant_text(raw_body)
        conv_id = _extract_conversation_id(raw_body)
        url = (
            f"https://grok.com/c/{conv_id}" if conv_id else self._client.GROK_HOME
        )
        return {
            "response": text,
            "url": url,
            "conversation_id": conv_id,
        }

    # ── list / read: trivially fast via the same shim ──────────────

    def list_conversations(self) -> list[dict]:
        """List the user's conversations via /rest/app-chat/conversations."""
        with self._client._session(self._client.DEFAULT_SESSION) as page:
            self._client._navigate_in(page, self._client.GROK_HOME, wait_ms=0)
            result = page.evaluate(
                """(url) => window.__UC_apiShim.fetch(url, {method: 'GET'})""",
                GROK_LIST_CHATS_URL,
            )
        if not result or not result.get("ok"):
            raise RuntimeError(
                f"GrokAPIClient.list: status={result.get('status') if result else 'none'}"
            )
        body = result.get("body") or ""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return []
        out = []
        for c in data.get("conversations") or []:
            cid = c.get("conversationId")
            if not cid:
                continue
            out.append(
                {
                    "id": cid,
                    "title": c.get("title"),
                    "url": f"https://grok.com/c/{cid}",
                }
            )
        return out

    def read(self, conversation_url: str) -> dict:
        """Fetch the response-node tree for a conversation."""
        conv_id = _conv_id_from_url(conversation_url)
        if not conv_id:
            raise ValueError(f"read: couldn't extract conv_id from {conversation_url!r}")
        endpoint = GROK_RESPONSE_NODE_URL.format(conv_id=conv_id)
        with self._client._session(self._client.DEFAULT_SESSION) as page:
            self._client._navigate_in(page, self._client.GROK_HOME, wait_ms=0)
            result = page.evaluate(
                """(url) => window.__UC_apiShim.fetch(url, {method: 'GET'})""",
                endpoint,
            )
        if not result or not result.get("ok"):
            raise RuntimeError(
                f"GrokAPIClient.read: status={result.get('status') if result else 'none'}"
            )
        body = result.get("body") or ""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return {"url": conversation_url, "conversation_id": conv_id, "messages": []}
        # response-node gives a flat list of {responseId, sender, parentResponseId}
        # We don't have the text content from this endpoint — that's in the
        # streamed send response or the conversation_v2 endpoint. Surface
        # the raw nodes for now; callers can resolve text later.
        return {
            "url": conversation_url,
            "conversation_id": conv_id,
            "messages": data.get("responseNodes") or [],
        }
