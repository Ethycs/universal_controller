"""Unit tests for the UCBrowser litellm CustomLLM provider.

GrokClient is mocked end-to-end — no browser is launched and no network
is touched. Tests exercise:

* model routing (uc/grok), reject unknown sites
* session continuity (session_id → reuses conversation_url)
* explicit conversation_url short-circuits the session store
* stream=True / empty messages / GrokAuthRequired error paths
* async path (acompletion)
* register_uc_provider idempotence + presence in litellm.custom_provider_map
* end-to-end litellm.completion call dispatches through the handler
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import litellm
import pytest

from uc_browser.llm_providers import uc as uc_mod
from uc_browser.llm_providers.uc import (
    PROVIDER,
    UCBrowserCustomLLM,
    _last_user_message,
    _reset_for_tests,
    register_uc_provider,
)
from uc_browser.sites.grok import GrokAuthRequired


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_registration():
    """Each test starts with a fresh registration state."""
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture
def fake_grok():
    """Patch the send orchestrator the provider imports as ``_grok_send``.

    The handler used to call ``get_grok_client().send(...)`` directly;
    it now goes through ``send_with_fallback`` (imported as ``_grok_send``)
    so the fast path is tried first. Tests assert against a MagicMock
    that stands in for that orchestrator — call args appear on
    ``fake_grok.send.call_args`` just like before so existing tests
    don't need to change.
    """
    client = MagicMock()
    client.send = MagicMock(
        return_value={
            "response": "hello back",
            "url": "https://grok.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "conversation_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }
    )
    with patch.object(uc_mod, "_grok_send", side_effect=client.send):
        yield client


@pytest.fixture
def handler(fake_grok) -> UCBrowserCustomLLM:
    return register_uc_provider()


def _fresh_model_response() -> litellm.ModelResponse:
    """Build a ModelResponse the way litellm would hand one to the handler."""
    return litellm.ModelResponse(
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": ""},
                "finish_reason": None,
            }
        ]
    )


# ── Pure helpers ─────────────────────────────────────────────────────


def test_last_user_message_picks_most_recent():
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "...response..."},
        {"role": "user", "content": "second"},
    ]
    assert _last_user_message(msgs) == "second"


def test_last_user_message_multimodal_concatenates_text_parts():
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image_url", "image_url": {"url": "..."}},
                {"type": "text", "text": "what is it?"},
            ],
        }
    ]
    assert _last_user_message(msgs) == "look at this\nwhat is it?"


def test_last_user_message_returns_empty_when_no_user_messages():
    assert _last_user_message([{"role": "assistant", "content": "hi"}]) == ""
    assert _last_user_message([]) == ""


# ── Registration ─────────────────────────────────────────────────────


def test_register_uc_provider_is_idempotent(fake_grok):
    h1 = register_uc_provider()
    h2 = register_uc_provider()
    assert h1 is h2
    entries = [
        e for e in (litellm.custom_provider_map or []) if e.get("provider") == PROVIDER
    ]
    assert len(entries) == 1
    assert entries[0]["custom_handler"] is h1


def test_register_uc_provider_replaces_stale_entry(fake_grok):
    # Pretend some old handler is already registered under the same key.
    sentinel = object()
    litellm.custom_provider_map = [{"provider": PROVIDER, "custom_handler": sentinel}]
    _reset_for_tests()  # clear our cache so the next call genuinely re-registers
    handler = register_uc_provider()
    entries = [
        e for e in litellm.custom_provider_map if e.get("provider") == PROVIDER
    ]
    assert len(entries) == 1
    assert entries[0]["custom_handler"] is handler  # not the sentinel


# ── Completion happy path ────────────────────────────────────────────


def test_completion_uses_last_user_message_and_returns_modelresponse(handler, fake_grok):
    mr = _fresh_model_response()
    out = handler.completion(
        model="grok",
        messages=[
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hello"},
        ],
        model_response=mr,
        optional_params={},
    )
    fake_grok.send.assert_called_once()
    assert fake_grok.send.call_args.args[0] == "hello"
    assert fake_grok.send.call_args.kwargs["conversation_url"] is None
    assert out is mr
    assert out.choices[0].message.content == "hello back"
    assert out.choices[0].finish_reason == "stop"
    assert out.model == "uc/grok"
    assert out._hidden_params["uc_conversation_url"].endswith("eeeeeeeeeeee")
    assert out._hidden_params["uc_conversation_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_completion_unsupported_model_raises_bad_request(handler):
    with pytest.raises(litellm.exceptions.BadRequestError):
        handler.completion(
            model="chatgpt",  # not in SUPPORTED_MODELS
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )


def test_streaming_emits_full_text_then_done_chunk(handler, fake_grok):
    """Streaming clients get the full response in one chunk + a finish chunk.

    Token-level streaming isn't available from a browser-driven backend, so
    we emit the populated ModelResponse as a single content chunk followed
    by the OpenAI-style ``is_finished=True`` sentinel.
    """
    chunks = list(
        handler.streaming(
            model="grok",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )
    )
    assert len(chunks) == 2
    assert chunks[0]["text"] == "hello back"
    assert chunks[0]["is_finished"] is False
    assert chunks[1]["text"] == ""
    assert chunks[1]["is_finished"] is True
    assert chunks[1]["finish_reason"] == "stop"


def test_astreaming_emits_full_text_then_done_chunk(handler, fake_grok):
    async def _collect():
        out = []
        async for c in handler.astreaming(
            model="grok",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        ):
            out.append(c)
        return out

    chunks = asyncio.run(_collect())
    assert len(chunks) == 2
    assert chunks[0]["text"] == "hello back"
    assert chunks[1]["is_finished"] is True


def test_completion_empty_messages_raises_bad_request(handler):
    with pytest.raises(litellm.exceptions.BadRequestError):
        handler.completion(
            model="grok",
            messages=[{"role": "system", "content": "be terse"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )


def test_completion_auth_required_maps_to_authentication_error(handler, fake_grok):
    fake_grok.send.side_effect = GrokAuthRequired("login needed")
    with pytest.raises(litellm.exceptions.AuthenticationError):
        handler.completion(
            model="grok",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )


# ── Session continuity ──────────────────────────────────────────────


def test_session_id_via_extra_body_continues_chat(handler, fake_grok):
    # First call: no stored URL → send() gets None and creates a chat.
    handler.completion(
        model="grok",
        messages=[{"role": "user", "content": "first"}],
        model_response=_fresh_model_response(),
        optional_params={"extra_body": {"session_id": "user-42"}},
    )
    assert fake_grok.send.call_args.kwargs["conversation_url"] is None
    stored = handler.get_session_url("user-42")
    assert stored and stored.endswith("eeeeeeeeeeee")

    # Second call with the same session_id reuses the stored URL.
    fake_grok.send.return_value = {
        "response": "follow-up",
        "url": stored,
        "conversation_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    }
    handler.completion(
        model="grok",
        messages=[{"role": "user", "content": "second"}],
        model_response=_fresh_model_response(),
        optional_params={"extra_body": {"session_id": "user-42"}},
    )
    assert fake_grok.send.call_args.kwargs["conversation_url"] == stored


def test_session_id_via_metadata_works_too(handler, fake_grok):
    handler.completion(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={},
        litellm_params={"litellm_session_id": "user-from-metadata"},
    )
    assert handler.get_session_url("user-from-metadata") is not None


def test_explicit_conversation_url_short_circuits_session_store(handler, fake_grok):
    # Pre-seed a stored URL; the explicit one should override it.
    handler.set_session_url("user-42", "https://grok.com/c/stored")
    handler.completion(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={
            "extra_body": {
                "session_id": "user-42",
                "conversation_url": "https://grok.com/c/explicit",
            }
        },
    )
    assert fake_grok.send.call_args.kwargs["conversation_url"] == "https://grok.com/c/explicit"


def test_forget_session_drops_stored_url(handler):
    handler.set_session_url("u", "https://grok.com/c/x")
    handler.forget_session("u")
    assert handler.get_session_url("u") is None


def test_no_session_no_store(handler, fake_grok):
    handler.completion(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={},
    )
    # No session key → nothing memoized.
    assert handler._sessions == {}


def test_session_id_is_forwarded_as_session_key_to_grok_client(handler, fake_grok):
    """Distinct session_ids must reach GrokClient.send() as distinct session_keys
    so each gets its own browser tab (enabling parallel sends)."""
    handler.completion(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={"extra_body": {"session_id": "user-A"}},
    )
    assert fake_grok.send.call_args.kwargs["session_key"] == "user-A"

    handler.completion(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={"extra_body": {"session_id": "user-B"}},
    )
    assert fake_grok.send.call_args.kwargs["session_key"] == "user-B"

    # No session_id → None reaches send(), which means the default shared tab.
    handler.completion(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={},
    )
    assert fake_grok.send.call_args.kwargs["session_key"] is None


def test_flattened_extras_from_proxy_are_honored(handler, fake_grok):
    """The litellm proxy flattens extra_body into optional_params top-level.

    Our resolvers must read both shapes so the same handler works for
    direct litellm.completion() calls and proxy-mediated calls.
    """
    handler.completion(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={
            "stream": False,
            # Flat, NOT nested under extra_body — this is what the proxy delivers.
            "session_id": "proxy-flat",
            "conversation_url": "https://grok.com/c/flat",
            "wait_for_response": False,
        },
    )
    # session_id was honored — session_url got persisted.
    assert handler.get_session_url("proxy-flat") is not None
    # conversation_url short-circuited the lookup.
    assert fake_grok.send.call_args.kwargs["conversation_url"] == "https://grok.com/c/flat"
    # wait_for_response=False propagated to GrokClient.send().
    assert fake_grok.send.call_args.kwargs["wait_for_response"] is False


# ── Async path ───────────────────────────────────────────────────────


def test_acompletion_runs_through_to_thread(handler, fake_grok):
    async def _go():
        return await handler.acompletion(
            model="grok",
            messages=[{"role": "user", "content": "async-hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )

    out = asyncio.run(_go())
    assert out.choices[0].message.content == "hello back"
    assert fake_grok.send.call_args.args[0] == "async-hi"


# ── End-to-end via litellm.completion() ──────────────────────────────


def test_litellm_completion_dispatches_to_handler(fake_grok):
    # Registration is what makes ``uc/grok`` resolvable.
    register_uc_provider()
    resp = litellm.completion(
        model="uc/grok",
        messages=[{"role": "user", "content": "ping"}],
    )
    assert resp.choices[0].message.content == "hello back"
    assert resp.model == "uc/grok"
    fake_grok.send.assert_called_once()
    assert fake_grok.send.call_args.args[0] == "ping"
