"""MCP tool registrations for Grok control.

Exposes 12 tools that delegate to a process-singleton GrokClient.
The client is created lazily on first call and reused thereafter; an
``atexit`` hook closes the underlying browser on shutdown.

Tools:
    Conversation: chat (POC: conversational handle keyed by session_id)
    Core:         grok_send_message, grok_read_conversation,
                  grok_list_conversations, grok_delete_conversation
    Lifecycle:    grok_new_chat, grok_rename_conversation,
                  grok_archive_conversation, grok_regenerate,
                  grok_stop_generation
    Models:       grok_get_models, grok_switch_model

The ``chat`` tool shares its session store with the litellm provider
(:data:`uc_browser.llm_providers.uc.uc_handler`) so the same
``session_id`` continues the same conversation whether the caller hits
the proxy or the MCP server.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from uc_browser.llm_providers.uc import uc_handler
from uc_browser.sites.grok import GrokAuthRequired, get_grok_client
from uc_browser.sites.grok_fast import send_with_fallback as _grok_send

logger = logging.getLogger("uc_browser.mcp.grok_tools")


def _wrap_auth(fn):
    """Decorator that converts GrokAuthRequired into a plain dict result.

    FastMCP serializes return values; raising would be ugly in the
    client. We instead return ``{"ok": False, "error": "...", ...}``.
    """

    def _inner(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GrokAuthRequired as exc:
            return {"ok": False, "error": "auth_required", "message": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Grok tool failed: %s", fn.__name__)
            return {"ok": False, "error": type(exc).__name__, "message": str(exc)}

    _inner.__name__ = fn.__name__
    _inner.__doc__ = fn.__doc__
    return _inner


def register_grok_tools(server) -> None:
    """Attach all Grok MCP tools to the given FastMCP ``server``."""

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
    @_wrap_auth
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
        existing_url = uc_handler.get_session_url(session_id)
        # send_with_fallback prefers the React-onSubmit + expect_response
        # fast path and only drops to DOM if the fast path fails. Blind
        # mode bypasses fast and goes straight to DOM.
        result = _grok_send(
            message,
            conversation_url=existing_url,
            timeout_s=timeout_s,
            wait_for_response=wait_for_response,
            # session_id is also the page bucket: distinct ids get
            # distinct tabs and can run concurrently.
            session_key=session_id,
        )
        if result.get("url"):
            uc_handler.set_session_url(session_id, result["url"])
        return {"ok": True, "session_id": session_id, **result}

    @server.tool()
    @_wrap_auth
    def grok_send_message(
        message: str,
        conversation_url: Optional[str] = None,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        """Send a message to Grok and return the assistant's response.

        If ``conversation_url`` is None, posts to https://grok.com/ which
        creates a new conversation. Returns ``{ok, response, url,
        conversation_id}``.
        """
        result = get_grok_client().send(
            message,
            conversation_url=conversation_url,
            timeout_s=timeout_s,
        )
        return {"ok": True, **result}

    @server.tool()
    @_wrap_auth
    def grok_read_conversation(conversation_url: str) -> dict[str, Any]:
        """Return the full transcript of a Grok conversation.

        Result: ``{ok, url, conversation_id, messages: [{role, text}, ...]}``.
        """
        result = get_grok_client().read(conversation_url)
        return {"ok": True, **result}

    @server.tool()
    @_wrap_auth
    def grok_list_conversations() -> dict[str, Any]:
        """List all conversations visible in the Grok sidebar.

        Result: ``{ok, conversations: [{id, title, url}, ...]}``.
        """
        items = get_grok_client().list_conversations()
        return {"ok": True, "conversations": items, "count": len(items)}

    @server.tool()
    @_wrap_auth
    def grok_delete_conversation(conversation_url: str) -> dict[str, Any]:
        """Delete a Grok conversation by URL or id.

        Result: ``{ok, deleted}`` where ``deleted`` reflects whether the
        sidebar row disappeared after the confirm-modal click.
        """
        ok = get_grok_client().delete(conversation_url)
        return {"ok": True, "deleted": ok}

    @server.tool()
    @_wrap_auth
    def grok_new_chat() -> dict[str, Any]:
        """Return the URL of the Grok new-chat surface.

        Grok creates a real conversation id only after the first message
        is sent. To materialize a conversation, follow this with
        ``grok_send_message`` (no ``conversation_url`` arg).
        """
        result = get_grok_client().new_chat()
        return {"ok": True, **result}

    @server.tool()
    @_wrap_auth
    def grok_rename_conversation(
        conversation_url: str,
        new_title: str,
    ) -> dict[str, Any]:
        """Rename a conversation. Result: ``{ok, renamed}``."""
        renamed = get_grok_client().rename(conversation_url, new_title)
        return {"ok": True, "renamed": renamed}

    @server.tool()
    @_wrap_auth
    def grok_archive_conversation(conversation_url: str) -> dict[str, Any]:
        """Archive a conversation. Result: ``{ok, archived}``.

        Note: Grok may not expose an explicit archive — recipe falls back
        to whatever "Archive" menu item is present. Returns ``False`` if
        no such item exists.
        """
        archived = get_grok_client().archive(conversation_url)
        return {"ok": True, "archived": archived}

    @server.tool()
    @_wrap_auth
    def grok_regenerate(
        conversation_url: str,
        timeout_s: int = 60,
    ) -> dict[str, Any]:
        """Regenerate the last assistant response. Returns ``{ok, response}``."""
        return get_grok_client().regenerate(conversation_url, timeout_s=timeout_s)

    @server.tool()
    @_wrap_auth
    def grok_stop_generation(conversation_url: str) -> dict[str, Any]:
        """Stop the assistant if it's currently generating. Returns ``{ok, stopped}``."""
        stopped = get_grok_client().stop(conversation_url)
        return {"ok": True, "stopped": stopped}

    @server.tool()
    @_wrap_auth
    def grok_get_models() -> dict[str, Any]:
        """Open the model picker and return available model names.

        Result: ``{ok, models: [...]}``.
        """
        models = get_grok_client().get_models()
        return {"ok": True, "models": models}

    @server.tool()
    @_wrap_auth
    def grok_switch_model(model_name: str) -> dict[str, Any]:
        """Open the model picker and select ``model_name``. Returns ``{ok, switched}``."""
        switched = get_grok_client().switch_model(model_name)
        return {"ok": True, "switched": switched}

    @server.tool()
    @_wrap_auth
    def list_chat_sessions() -> dict[str, Any]:
        """List session_ids currently mapped to a Grok conversation.

        Useful when you've called ``chat`` several times and need to see
        which session_ids are still "alive" in the in-memory store.
        Returns ``{ok, sessions: {session_id: url, ...}}``.
        """
        with uc_handler._sessions_lock:
            snapshot = dict(uc_handler._sessions)
        return {"ok": True, "sessions": snapshot, "count": len(snapshot)}

    @server.tool()
    @_wrap_auth
    def forget_chat_session(session_id: str) -> dict[str, Any]:
        """Drop a session_id from the chat session store.

        The next ``chat`` call with this session_id will start a fresh
        Grok conversation. The Grok-side chat itself is NOT deleted —
        use ``grok_delete_conversation`` for that.

        Returns ``{ok, forgotten}``.
        """
        before = uc_handler.get_session_url(session_id)
        uc_handler.forget_session(session_id)
        return {"ok": True, "forgotten": before is not None, "previous_url": before}
