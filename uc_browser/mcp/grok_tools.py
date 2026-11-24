"""MCP tool registrations for Grok control.

Exposes the Grok tools as thin clients of the UC service's ``/sites/grok``
routes. The service owns the browser, the session map and the Grok folder
policy; these tools hold no browser and no session state.

Tools:
    Conversation: chat (conversational handle keyed by session_id)
    Core:         grok_send_message, grok_read_conversation,
                  grok_list_conversations, grok_delete_conversation
    Lifecycle:    grok_new_chat, grok_rename_conversation,
                  grok_archive_conversation, grok_regenerate,
                  grok_stop_generation
    Models:       grok_get_models, grok_switch_model
    Sessions:     list_chat_sessions, forget_chat_session

The service address is explicit: ``register_grok_tools(server,
service_url=...)`` or the consumer's ``UC_GROK_SERVICE_URL``. With neither,
every tool answers ``{"ok": False, "error": "service_not_configured"}``;
nothing opens a local browser.

``chat`` uses the service session named by ``session_id`` — the same map the
litellm provider uses for an explicit session id — so one id continues one
conversation whether the caller hits the proxy or the MCP server.
``grok_send_message`` runs in a unique session sent with ``ephemeral=True``
(the service keeps no persistent map entry for it) and forgets it once the
reply is complete, so it can never continue the ``default`` chat. It always
waits for the reply, so it never leaves a blind send behind. Every other tool
runs in the shared internal admin session.
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any, Optional

from uc_browser.llm_providers.uc import (
    ADMIN_SESSION,
    GROK_SERVICE_URL_ENV,
    GROK_SITE,
    INTERNAL_SESSION_PREFIX,
    ephemeral_session_id,
    forget_service_session,
    is_internal_session,
)
from uc_browser.service.client import ServiceClient, ServiceError

logger = logging.getLogger("uc_browser.mcp.grok_tools")


class GrokServiceNotConfigured(RuntimeError):
    """No UC service address was given for the Grok tools."""


def _wrap_errors(fn):
    """Decorator that turns service failures into a plain dict result.

    FastMCP serializes return values; raising would be ugly in the
    client. We instead return ``{"ok": False, "error": "...", ...}``.
    """

    @functools.wraps(fn)
    def _inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GrokServiceNotConfigured as exc:
            return {"ok": False, "error": "service_not_configured", "message": str(exc)}
        except ServiceError as exc:
            return {"ok": False, "error": exc.code, "message": exc.detail}
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Grok tool failed: %s", fn.__name__)
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}

    return _inner


def _shaped(result: Any, kind: type, op: str) -> Any:
    """The service's result, checked against the shape this tool returns."""
    if not isinstance(result, kind):
        raise ServiceError(
            "bad_response",
            f"{op} returned {type(result).__name__}, expected {kind.__name__}",
            502,
        )
    return result


def _chat_session(session_id: str) -> str:
    if is_internal_session(session_id):
        raise ValueError(
            f"session ids starting with {INTERNAL_SESSION_PREFIX!r} are reserved"
        )
    return session_id


def register_grok_tools(server, *, service_url: Optional[str] = None) -> None:
    """Attach all Grok MCP tools to the given FastMCP ``server``.

    ``service_url`` names the UC service; when omitted the consumer's
    ``UC_GROK_SERVICE_URL`` is used. Registration succeeds without either —
    the tools then report ``service_not_configured`` when called."""
    url = (service_url or os.environ.get(GROK_SERVICE_URL_ENV) or "").strip()
    client = ServiceClient(url) if url else None

    def _client() -> ServiceClient:
        if client is None:
            raise GrokServiceNotConfigured(
                "no UC service is configured for the Grok tools. Pass "
                f"service_url to register_grok_tools or set {GROK_SERVICE_URL_ENV} "
                "to the service's URL (for example http://127.0.0.1:4010). "
                "There is no local-browser fallback."
            )
        return client

    def _admin(op: str, kwargs: Optional[dict] = None) -> Any:
        return _client().site(GROK_SITE, op, kwargs, session_id=ADMIN_SESSION)

    @server.prompt(
        name="talk_to_grok",
        title="Talk to this chat tool if necessary",
        description=(
            "Invites the calling LLM to use the `chat` tool for back-and-forth "
            "conversation with a browser-driven Grok session."
        ),
    )
    def talk_to_grok_prompt(session_id: str = "default") -> str:
        """Prompt template the client can fetch and feed to its LLM."""
        return (
            "Talk to this chat tool if necessary.\n\n"
            "You can have a multi-turn conversation with Grok by calling the\n"
            "`chat` tool. The tool takes a `message` (your prompt) and a\n"
            f"`session_id` (use `{session_id}` to keep this conversation\n"
            "going across calls, or pick a different one to start a fresh\n"
            "thread). The reply is returned synchronously. Pass\n"
            "`wait_for_response=False` if you just want to fire a prompt\n"
            "without waiting for the answer.\n\n"
            "When you need information Grok can give you, or when the user\n"
            "wants to talk to Grok, route the request through `chat`."
        )

    @server.tool()
    @_wrap_errors
    def chat(
        message: str,
        session_id: str = "default",
        wait_for_response: bool = True,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        """Have a conversation with this tool.

        This is a conversational handle on Grok. Send a message, get a
        reply. Pass the same ``session_id`` across calls to continue the
        same chat; use a fresh ``session_id`` to start a new one. The
        first call for a given ``session_id`` opens a new Grok chat; every
        subsequent call posts into the same one and the assistant has
        full context of what's been said.

        Set ``wait_for_response=False`` for fire-and-forget mode: the call
        returns as soon as the message is submitted (~1.6 s) and
        ``response`` will be empty. Useful for batching many prompts when
        you don't need to read each reply.

        Returns:
            ``{ok, response, url, conversation_id, session_id}``.
            ``response`` is the assistant's reply text (Grok's "Thought
            for Ns" reasoning prefix is stripped). ``url`` is the canonical
            grok.com/c/<uuid> URL; ``conversation_id`` is the bare uuid.
        """
        # The service session named by session_id owns the page and the
        # conversation map: it continues its own conversation.
        result = _client().site(
            GROK_SITE,
            "send",
            {
                "message": message,
                "timeout_s": timeout_s,
                "wait_for_response": wait_for_response,
            },
            session_id=_chat_session(session_id),
        )
        return {"ok": True, "session_id": session_id, **_shaped(result, dict, "send")}

    @server.tool()
    @_wrap_errors
    def grok_send_message(
        message: str,
        conversation_url: Optional[str] = None,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        """Send a message to Grok and return the assistant's response.

        If ``conversation_url`` is None, starts a new conversation (in the
        service's configured Grok folder); it never continues a ``chat``
        session. Returns ``{ok, response, url, conversation_id}``.
        """
        send_kwargs: dict[str, Any] = {"message": message, "timeout_s": timeout_s}
        if conversation_url is not None:
            # Sent as provided: the service validates it against its folder.
            send_kwargs["conversation_url"] = conversation_url
        service = _client()
        session_id = ephemeral_session_id()
        try:
            result = service.site(
                GROK_SITE, "send", send_kwargs, session_id=session_id, ephemeral=True,
            )
        finally:
            forget_service_session(service, session_id)
        return {"ok": True, **_shaped(result, dict, "send")}

    @server.tool()
    @_wrap_errors
    def grok_read_conversation(conversation_url: str) -> dict[str, Any]:
        """Return the full transcript of a Grok conversation.

        Result: ``{ok, url, conversation_id, messages: [{role, text}, ...]}``.
        """
        result = _admin("read", {"conversation_url": conversation_url})
        return {"ok": True, **_shaped(result, dict, "read")}

    @server.tool()
    @_wrap_errors
    def grok_list_conversations() -> dict[str, Any]:
        """List the conversations of the service's Grok folder.

        Result: ``{ok, conversations: [{id, title, url}, ...]}``.
        """
        items = _shaped(_admin("list_conversations"), list, "list_conversations")
        return {"ok": True, "conversations": items, "count": len(items)}

    @server.tool()
    @_wrap_errors
    def grok_delete_conversation(conversation_url: str) -> dict[str, Any]:
        """Delete a Grok conversation by URL or id.

        Result: ``{ok, deleted}`` where ``deleted`` reflects whether the
        sidebar row disappeared after the confirm-modal click. The service
        refuses conversations outside its configured folder.
        """
        ok = _shaped(_admin("delete", {"conversation_url": conversation_url}), bool, "delete")
        return {"ok": True, "deleted": ok}

    @server.tool()
    @_wrap_errors
    def grok_new_chat() -> dict[str, Any]:
        """Return the URL of the Grok new-chat surface.

        Grok creates a real conversation id only after the first message
        is sent. To materialize a conversation, follow this with
        ``grok_send_message`` (no ``conversation_url`` arg).
        """
        return {"ok": True, **_shaped(_admin("new_chat"), dict, "new_chat")}

    @server.tool()
    @_wrap_errors
    def grok_rename_conversation(
        conversation_url: str,
        new_title: str,
    ) -> dict[str, Any]:
        """Rename a conversation. Result: ``{ok, renamed}``."""
        renamed = _shaped(
            _admin("rename", {"conversation_url": conversation_url, "new_title": new_title}),
            bool, "rename",
        )
        return {"ok": True, "renamed": renamed}

    @server.tool()
    @_wrap_errors
    def grok_archive_conversation(conversation_url: str) -> dict[str, Any]:
        """Archive a conversation. Result: ``{ok, archived}``.

        Note: Grok may not expose an explicit archive — recipe falls back
        to whatever "Archive" menu item is present. Returns ``False`` if
        no such item exists.
        """
        archived = _shaped(_admin("archive", {"conversation_url": conversation_url}), bool, "archive")
        return {"ok": True, "archived": archived}

    @server.tool()
    @_wrap_errors
    def grok_regenerate(
        conversation_url: str,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        """Regenerate the last assistant response. Returns ``{ok, response}``."""
        result = _admin(
            "regenerate", {"conversation_url": conversation_url, "timeout_s": timeout_s},
        )
        return _shaped(result, dict, "regenerate")

    @server.tool()
    @_wrap_errors
    def grok_stop_generation(conversation_url: str) -> dict[str, Any]:
        """Stop the assistant if it's currently generating. Returns ``{ok, stopped}``."""
        # No session id: the service finds the busy page by conversation URL
        # and acts on it without queueing behind the send.
        result = _client().site(GROK_SITE, "stop", {"conversation_url": conversation_url})
        return {"ok": True, "stopped": _shaped(result, bool, "stop")}

    @server.tool()
    @_wrap_errors
    def grok_get_models() -> dict[str, Any]:
        """Open the model picker and return available model names.

        Result: ``{ok, models: [...]}``.
        """
        return {"ok": True, "models": _shaped(_admin("get_models"), list, "get_models")}

    @server.tool()
    @_wrap_errors
    def grok_switch_model(model_name: str) -> dict[str, Any]:
        """Open the model picker and select ``model_name``. Returns ``{ok, switched}``."""
        switched = _shaped(_admin("switch_model", {"model_name": model_name}), bool, "switch_model")
        return {"ok": True, "switched": switched}

    @server.tool()
    @_wrap_errors
    def list_chat_sessions() -> dict[str, Any]:
        """List session_ids currently mapped to a Grok conversation.

        Useful when you've called ``chat`` several times and need to see
        which session_ids the service still maps to a conversation.
        Returns ``{ok, sessions: {session_id: url, ...}}``.
        """
        mapped = _client().site_sessions(GROK_SITE)
        snapshot = {
            sid: info["url"]
            for sid, info in mapped.items()
            if not is_internal_session(sid) and info.get("url")
        }
        return {"ok": True, "sessions": snapshot, "count": len(snapshot)}

    @server.tool()
    @_wrap_errors
    def forget_chat_session(session_id: str) -> dict[str, Any]:
        """Drop a session_id from the service's chat session map.

        The next ``chat`` call with this session_id will start a fresh
        Grok conversation. The Grok-side chat itself is NOT deleted —
        use ``grok_delete_conversation`` for that.

        Returns ``{ok, forgotten, previous_url}``.
        """
        service = _client()
        _chat_session(session_id)
        previous = (service.site_sessions(GROK_SITE).get(session_id) or {}).get("url")
        try:
            service.forget_session(GROK_SITE, session_id)
        except ServiceError as exc:
            if exc.code != "no_such_session":
                raise
            return {"ok": True, "forgotten": False, "previous_url": previous}
        return {"ok": True, "forgotten": True, "previous_url": previous}
