"""Unit tests for the UCBrowser litellm CustomLLM provider.

Grok is a thin client of the UC service: the provider is given a fake
``ServiceClient`` that records every ``site`` / ``forget_session`` call, so
no browser, service or network is involved (the real HTTP wire is covered by
``test_grok_service_clients.py``). Tests exercise:

* the wire request a completion makes (op, kwargs, session id)
* persistent sessions for explicit ids, unique ephemeral sessions otherwise
* real streaming with final-result reconciliation
* delete-after-capture and cleanup_threads as single service requests
* ServiceError -> litellm error mapping, with no resend
* the explicit service address and the absence of a local fallback
* model routing, registration, backup model and the generic engine route
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import litellm
import pytest

from uc_browser.llm_providers.uc import (
    PROVIDER,
    UCBrowserCustomLLM,
    _last_user_message,
    _reset_for_tests,
    register_uc_provider,
)
from uc_browser.service.client import ServiceError, UCServiceGone

CID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
URL = f"https://grok.com/c/{CID}"


# ── Fixtures ─────────────────────────────────────────────────────────


class FakeStream:
    """What ``ServiceClient.site(stream=True)`` returns: a context manager
    over ``{"delta"}`` lines then ``{"result"}``."""

    def __init__(self, items, gate=None, error=None):
        self.items = items
        self.gate = gate
        self.error = error
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def __iter__(self):
        for i, item in enumerate(self.items):
            if i == 1 and self.gate is not None:
                assert self.gate.wait(5), "the test never released the stream"
            yield item
        if self.error is not None:
            raise self.error


class FakeService:
    """Duck-typed ``ServiceClient`` recording what the provider asks for."""

    def __init__(self):
        self.calls: list[SimpleNamespace] = []
        self.forgotten: list[tuple[str, str]] = []
        self.send_result = {"response": "hello back", "url": URL, "conversation_id": CID}
        self.results: dict = {}
        self.errors: dict = {}
        self.forget_error = None
        self.deltas: list[str] = []
        self.emit_result = True
        self.stream_error = None
        self.gate = None
        self.last_stream = None

    def sends(self):
        return [c for c in self.calls if c.op == "send"]

    def site(self, site, op, kwargs=None, *, session_id=None, stream=False, wait=None,
             ephemeral=False, timeout=None):
        self.calls.append(SimpleNamespace(
            site=site, op=op, kwargs=dict(kwargs or {}), session_id=session_id, stream=stream,
            ephemeral=ephemeral,
        ))
        if op in self.errors:
            raise self.errors[op]
        result = self.results.get(op, self.send_result if op == "send" else None)
        if not stream:
            return result
        items = [{"delta": d} for d in self.deltas]
        # An error line takes the place of the result line.
        if self.emit_result and self.stream_error is None:
            items.append({"result": result})
        self.last_stream = FakeStream(items, self.gate, self.stream_error)
        return self.last_stream

    def forget_session(self, site, session_id):
        self.forgotten.append((site, session_id))
        if self.forget_error is not None:
            raise self.forget_error
        return {"forgotten": True}


@pytest.fixture(autouse=True)
def _clean_registration(monkeypatch):
    """Each test starts with a fresh registration state and no ambient
    health data or backup model deciding the outcome."""
    monkeypatch.setenv("UC_AVAILABILITY_GATE", "0")
    monkeypatch.delenv("UC_BACKUP_MODEL", raising=False)
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def handler(service) -> UCBrowserCustomLLM:
    return UCBrowserCustomLLM(client=service)


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


def _ask(handler, text="hi", optional_params=None, litellm_params=None, timeout=None):
    return handler.completion(
        model="grok",
        messages=[{"role": "user", "content": text}],
        model_response=_fresh_model_response(),
        optional_params=optional_params or {},
        litellm_params=litellm_params,
        timeout=timeout,
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


def test_register_uc_provider_is_idempotent():
    h1 = register_uc_provider()
    h2 = register_uc_provider()
    assert h1 is h2
    entries = [
        e for e in (litellm.custom_provider_map or []) if e.get("provider") == PROVIDER
    ]
    assert len(entries) == 1
    assert entries[0]["custom_handler"] is h1


def test_register_uc_provider_replaces_stale_entry():
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


def test_completion_uses_last_user_message_and_returns_modelresponse(handler, service, monkeypatch):
    monkeypatch.delenv("UC_GROK_TIMEOUT_S", raising=False)
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
    (call,) = service.calls
    assert (call.site, call.op, call.stream) == ("grok", "send", False)
    assert call.kwargs == {"message": "hello", "timeout_s": 300, "wait_for_response": True}
    assert out is mr
    assert out.choices[0].message.content == "hello back"
    assert out.choices[0].finish_reason == "stop"
    assert out.model == "uc/grok"
    assert out._hidden_params["uc_conversation_url"] == URL
    assert out._hidden_params["uc_conversation_id"] == CID


@pytest.mark.parametrize("requested, sent", [(1000, 1000), (5, 300), (None, 300)])
def test_service_wait_is_floored_at_the_grok_timeout(handler, service, monkeypatch, requested, sent):
    monkeypatch.delenv("UC_GROK_TIMEOUT_S", raising=False)
    _ask(handler, timeout=requested)
    assert service.sends()[0].kwargs["timeout_s"] == sent


def test_completion_unsupported_model_raises_bad_request(handler):
    with pytest.raises(litellm.exceptions.BadRequestError):
        handler.completion(
            # Neither adapter-backed nor a registry site with a generic
            # litellm model.
            model="definitely-not-a-site",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )


def test_completion_generic_site_routes_through_generic_client(handler, monkeypatch):
    """Registry sites advertised as uc/<name> (e.g. chatgpt) dispatch to
    the generic engine client, not the Grok adapter."""
    from uc_browser.sites import generic as generic_mod

    # Isolate from whatever live health data this machine has.
    monkeypatch.setenv("UC_AVAILABILITY_GATE", "0")

    fake = MagicMock()
    fake.send.return_value = {
        "response": "generic hello",
        "site": "chatgpt",
        "page_url": "https://chatgpt.com/",
    }
    with patch.object(generic_mod, "_client", fake), \
         patch.object(generic_mod, "get_generic_client", return_value=fake):
        out = handler.completion(
            model="chatgpt",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={"extra_body": {"session_id": "g-1"}},
        )
    fake.send.assert_called_once()
    args, kwargs = fake.send.call_args
    assert args[0] == "chatgpt" and args[1] == "https://chatgpt.com"
    assert kwargs["session_key"] == "g-1"
    assert out.choices[0].message.content == "generic hello"
    assert out.model == "uc/chatgpt"


# ── Streaming ────────────────────────────────────────────────────────


def _texts(chunks):
    return [c["text"] for c in chunks]


def _assert_finished(final):
    assert final["is_finished"] is True
    assert final["finish_reason"] == "stop"
    assert final["text"] == ""
    assert final["usage"]["total_tokens"] > 0


def test_streaming_forwards_deltas_while_the_reply_is_still_rendering(handler, service):
    service.deltas = ["Hel", "lo"]
    service.send_result["response"] = "Hello"
    service.gate = threading.Event()
    chunks = handler.streaming(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={},
    )
    # The service is still holding back the rest of the stream.
    first = next(chunks)
    assert first["text"] == "Hel" and first["is_finished"] is False
    assert not service.gate.is_set()
    service.gate.set()
    rest = list(chunks)
    assert _texts(rest[:-1]) == ["lo"]
    _assert_finished(rest[-1])
    assert service.calls[0].stream is True


def test_streaming_reconciles_text_the_deltas_did_not_carry(handler, service):
    service.deltas = ["Hel"]
    service.send_result["response"] = "Hello"
    chunks = list(handler.streaming(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={},
    ))
    assert _texts(chunks[:-1]) == ["Hel", "lo"]
    _assert_finished(chunks[-1])


def test_streaming_without_deltas_emits_the_result_as_one_chunk(handler, service):
    chunks = list(handler.streaming(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={},
    ))
    assert _texts(chunks[:-1]) == ["hello back"]
    _assert_finished(chunks[-1])
    # A streamed request is still an ephemeral session, forgotten afterwards.
    assert service.forgotten == [("grok", service.sends()[0].session_id)]


def test_streaming_raises_the_mapped_error_after_the_deltas_and_closes_the_stream(handler, service):
    service.deltas = ["par"]
    service.stream_error = ServiceError("auth_required", "signed out", 401)
    chunks = handler.streaming(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={},
    )
    assert next(chunks)["text"] == "par"
    with pytest.raises(litellm.exceptions.AuthenticationError):
        next(chunks)
    assert service.last_stream.closed
    assert len(service.sends()) == 1  # never resent


def test_a_stream_that_ends_without_a_result_is_an_error_not_a_guess(handler, service):
    service.deltas = ["par"]
    service.emit_result = False
    chunks = handler.streaming(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={},
    )
    assert next(chunks)["text"] == "par"
    with pytest.raises(litellm.exceptions.APIError, match="stream_truncated"):
        next(chunks)
    assert len(service.sends()) == 1


def test_astreaming_forwards_deltas_live_then_reconciles(handler, service):
    service.deltas = ["Hel"]
    service.send_result["response"] = "Hello"
    service.gate = threading.Event()

    async def _go():
        agen = handler.astreaming(
            model="grok",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )
        out = [await agen.__anext__()]
        live = not service.gate.is_set()
        service.gate.set()
        async for chunk in agen:
            out.append(chunk)
        return live, out

    live, chunks = asyncio.run(_go())
    assert live
    assert _texts(chunks[:-1]) == ["Hel", "lo"]
    _assert_finished(chunks[-1])


def test_astreaming_raises_the_mapped_error(handler, service):
    service.errors["send"] = ServiceError("timeout", "partial", 504)

    async def _go():
        return [c async for c in handler.astreaming(
            model="grok",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )]

    with pytest.raises(litellm.exceptions.Timeout):
        asyncio.run(_go())


def test_completion_empty_messages_raises_bad_request(handler, service):
    with pytest.raises(litellm.exceptions.BadRequestError):
        handler.completion(
            model="grok",
            messages=[{"role": "system", "content": "be terse"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )
    assert service.calls == []


# ── Service failures map to litellm errors, never a resend ──────────


@pytest.mark.parametrize(
    "error, expected",
    [
        (ServiceError("auth_required", "signed out", 401), litellm.exceptions.AuthenticationError),
        (ServiceError("timeout", "secret partial text", 504), litellm.exceptions.Timeout),
        (TimeoutError("read timed out"), litellm.exceptions.Timeout),
        (ServiceError("busy", "send in flight", 503), litellm.exceptions.ServiceUnavailableError),
        (ServiceError("page_deviation", "composer missing", 409), litellm.exceptions.ServiceUnavailableError),
        (ServiceError("folder_mismatch", "outside folder", 409), litellm.exceptions.BadRequestError),
        (UCServiceGone("connection refused"), litellm.exceptions.ServiceUnavailableError),
        (ServiceError("session_lost", "page vanished", 503), litellm.exceptions.ServiceUnavailableError),
        (ServiceError("session_closed", "closed", 409), litellm.exceptions.ServiceUnavailableError),
        (ServiceError("session_unavailable", "evicting", 503), litellm.exceptions.ServiceUnavailableError),
        (ServiceError("cancelled", "shutdown", 409), litellm.exceptions.ServiceUnavailableError),
        (ServiceError("session_conflict", "ephemeral mismatch", 409), litellm.exceptions.BadRequestError),
        (ServiceError("site_failed", "adapter failed", 500), litellm.exceptions.APIError),
        (ServiceError("session_store_failed", "disk", 500), litellm.exceptions.APIError),
        (ServiceError("boom", "unexpected", 500), litellm.exceptions.APIError),
    ],
)
def test_service_failures_map_to_litellm_errors_without_resending(handler, service, error, expected):
    service.errors["send"] = error
    with pytest.raises(expected) as caught:
        _ask(handler)
    # Partial text is account content: it must not leak into the message.
    assert "secret partial text" not in str(caught.value)
    assert len(service.sends()) == 1


def test_auth_required_falls_back_to_the_backup_model_when_one_is_configured(
    handler, service, monkeypatch,
):
    monkeypatch.setenv("UC_AVAILABILITY_GATE", "0")
    monkeypatch.setenv("UC_BACKUP_MODEL", "gpt-4o-mini")
    service.errors["send"] = ServiceError("auth_required", "signed out", 401)
    backup_resp = _fresh_model_response()
    backup_resp.choices[0].message.content = "backup answer"
    with patch.object(litellm, "completion", return_value=backup_resp):
        out = _ask(handler)
    assert out.choices[0].message.content == "backup answer"
    assert out._hidden_params["uc_backup_used"] == "gpt-4o-mini"
    assert "auth required" in out._hidden_params["uc_backup_reason"]
    assert len(service.sends()) == 1


# ── Explicit service address, no local fallback ─────────────────────


def test_grok_without_a_service_address_fails_with_an_actionable_error():
    unconfigured = UCBrowserCustomLLM()
    with pytest.raises(litellm.exceptions.ServiceUnavailableError, match="UC_GROK_SERVICE_URL"):
        _ask(unconfigured)


def test_service_url_and_client_are_mutually_exclusive(service):
    with pytest.raises(ValueError):
        UCBrowserCustomLLM("http://127.0.0.1:4010", client=service)


def test_a_malformed_service_url_is_rejected_at_construction():
    with pytest.raises(ValueError):
        UCBrowserCustomLLM("127.0.0.1:4010")


# ── Sessions are owned by the service ───────────────────────────────


def test_explicit_session_id_names_a_persistent_service_session(handler, service):
    for text in ("first", "second"):
        _ask(handler, text, optional_params={"extra_body": {"session_id": "user-42"}})
    first, second = service.sends()
    assert first.session_id == second.session_id == "user-42"
    # The service session continues its own conversation: no URL is sent and
    # nothing is forgotten, so the next call finds the same chat.
    assert "conversation_url" not in first.kwargs
    assert "conversation_url" not in second.kwargs
    assert service.forgotten == []
    assert first.ephemeral is False and second.ephemeral is False


def test_session_id_via_metadata_works_too(handler, service):
    _ask(handler, litellm_params={"litellm_session_id": "user-from-metadata"})
    assert service.sends()[0].session_id == "user-from-metadata"
    assert service.forgotten == []


def test_requests_without_a_session_get_unique_ephemeral_sessions(handler, service):
    _ask(handler)
    _ask(handler)
    first, second = service.sends()
    assert first.session_id != second.session_id
    assert all(c.session_id.startswith("_uc-tmp-") for c in (first, second))
    # Each ephemeral session is forgotten exactly once, after its send. That
    # drops the service's page and map entry, never the account conversation.
    assert service.forgotten == [("grok", first.session_id), ("grok", second.session_id)]
    assert [c.op for c in service.calls] == ["send", "send"]


def test_ephemeral_session_is_forgotten_even_when_the_send_fails(handler, service):
    service.errors["send"] = ServiceError("busy", "account busy", 503)
    with pytest.raises(litellm.exceptions.ServiceUnavailableError):
        _ask(handler)
    assert service.forgotten == [("grok", service.sends()[0].session_id)]


def test_ephemeral_sessions_are_marked_ephemeral_and_explicit_ones_are_not(handler, service):
    _ask(handler)
    _ask(handler, optional_params={"session_id": "user-42"})
    ephemeral_send, persistent_send = service.sends()
    assert ephemeral_send.ephemeral is True
    assert persistent_send.ephemeral is False


def test_a_blind_ephemeral_request_leaves_its_page_alive_for_idle_expiry(handler, service):
    """wait_for_response=False returns right after submit; closing the page
    then would cut off a generation nobody watched. The service discards the
    ephemeral session itself when it goes idle."""
    out = _ask(handler, optional_params={"wait_for_response": False})
    (send,) = service.sends()
    assert send.ephemeral is True and send.kwargs["wait_for_response"] is False
    assert service.forgotten == []
    assert out.choices[0].finish_reason == "stop"


def test_a_blind_ephemeral_stream_and_a_blind_failure_are_not_forgotten_either(handler, service):
    list(handler.streaming(
        model="grok",
        messages=[{"role": "user", "content": "hi"}],
        model_response=_fresh_model_response(),
        optional_params={"wait_for_response": False},
    ))
    assert service.sends()[0].stream is True and service.sends()[0].ephemeral is True
    service.errors["send"] = ServiceError("timeout", "", 504)
    with pytest.raises(litellm.exceptions.Timeout):
        _ask(handler, optional_params={"wait_for_response": False})
    # The message may already have been submitted: nothing is closed under it.
    assert service.forgotten == []


def test_a_failing_forget_never_masks_the_reply(handler, service):
    service.forget_error = ServiceError("no_such_session", "gone", 404)
    assert _ask(handler).choices[0].message.content == "hello back"
    service.forget_error = UCServiceGone("down")
    assert _ask(handler).choices[0].message.content == "hello back"


def test_explicit_conversation_url_is_sent_as_provided(handler, service):
    _ask(handler, optional_params={"extra_body": {
        "session_id": "user-42",
        "conversation_url": "https://grok.com/c/explicit",
    }})
    call = service.sends()[0]
    assert call.session_id == "user-42"
    assert call.kwargs["conversation_url"] == "https://grok.com/c/explicit"


def test_explicit_conversation_url_without_a_session_stays_ephemeral(handler, service):
    _ask(handler, optional_params={"conversation_url": "https://grok.com/c/explicit"})
    call = service.sends()[0]
    assert call.session_id.startswith("_uc-tmp-")
    assert call.kwargs["conversation_url"] == "https://grok.com/c/explicit"
    assert service.forgotten == [("grok", call.session_id)]


def test_flattened_extras_from_proxy_are_honored(handler, service):
    """The litellm proxy flattens extra_body into optional_params top-level.

    Our resolvers must read both shapes so the same handler works for
    direct litellm.completion() calls and proxy-mediated calls.
    """
    _ask(handler, optional_params={
        "stream": False,
        # Flat, NOT nested under extra_body — this is what the proxy delivers.
        "session_id": "proxy-flat",
        "conversation_url": "https://grok.com/c/flat",
        "wait_for_response": False,
    })
    call = service.sends()[0]
    assert call.session_id == "proxy-flat"
    assert call.kwargs["conversation_url"] == "https://grok.com/c/flat"
    assert call.kwargs["wait_for_response"] is False


# ── Delete-after-capture ─────────────────────────────────────────────


@pytest.fixture
def delete_after_capture(monkeypatch):
    monkeypatch.setenv("UC_GROK_DELETE_AFTER_CAPTURE", "1")


def test_delete_after_capture_deletes_through_the_service(handler, service, delete_after_capture):
    service.results["delete"] = True
    _ask(handler, optional_params={"extra_body": {"session_id": "s1"}})
    send, delete = service.calls
    assert (delete.op, delete.kwargs, delete.session_id, delete.ephemeral) == (
        "delete", {"conversation_url": URL}, "s1", False)
    # The conversation is gone: the persistent session must not continue it.
    assert service.forgotten == [("grok", "s1")]


def test_delete_after_capture_on_an_ephemeral_session_forgets_it_once(
    handler, service, delete_after_capture,
):
    service.results["delete"] = True
    _ask(handler)
    assert [c.op for c in service.calls] == ["send", "delete"]
    # The follow-on delete belongs to the same ephemeral session and says so.
    assert service.calls[1].session_id == service.sends()[0].session_id
    assert service.calls[1].ephemeral is True
    assert service.forgotten == [("grok", service.sends()[0].session_id)]


def test_delete_after_capture_keeps_the_session_when_nothing_was_deleted(
    handler, service, delete_after_capture,
):
    service.results["delete"] = False
    _ask(handler, optional_params={"extra_body": {"session_id": "s1"}})
    assert service.forgotten == []


def test_delete_after_capture_skips_an_empty_reply(handler, service, delete_after_capture):
    service.results["send"] = {"response": "  ", "url": URL, "conversation_id": CID}
    _ask(handler)
    assert [c.op for c in service.calls] == ["send"]


def test_delete_after_capture_failure_never_blocks_the_reply(handler, service, delete_after_capture):
    service.errors["delete"] = ServiceError("folder_mismatch", "not in folder", 409)
    assert _ask(handler).choices[0].message.content == "hello back"


def test_nothing_is_deleted_unless_asked(handler, service, monkeypatch):
    monkeypatch.delenv("UC_GROK_DELETE_AFTER_CAPTURE", raising=False)
    _ask(handler)
    assert [c.op for c in service.calls] == ["send"]


# ── cleanup_threads is one service request ───────────────────────────


def test_cleanup_threads_asks_the_service_once_and_sends_nothing(handler, service):
    service.results["cleanup_threads"] = {
        "summary": "kept 2, deleted 3", "deleted_ids": ["c1", "c2", "c3"]}
    out = handler.completion(
        model="grok",
        messages=[],
        model_response=_fresh_model_response(),
        optional_params={"extra_body": {"action": "cleanup_threads", "keep_recent": 2}},
    )
    (call,) = service.calls
    assert (call.op, call.kwargs, call.session_id) == (
        "cleanup_threads", {"keep_recent": 2}, "_uc-admin")
    assert out.choices[0].message.content == "kept 2, deleted 3"


def test_cleanup_threads_defaults_to_keeping_one(handler, service):
    service.results["cleanup_threads"] = {"summary": "kept 1, deleted 4", "deleted_ids": []}
    out = handler.completion(
        model="grok",
        messages=[],
        model_response=_fresh_model_response(),
        optional_params={"action": "cleanup_threads", "keep_recent": "not-a-number"},
    )
    assert service.calls[0].kwargs == {"keep_recent": 1}
    assert out.choices[0].message.content == "kept 1, deleted 4"


@pytest.mark.parametrize(
    "result", ["kept 2, deleted 3", {"deleted_ids": ["a"]}, {"summary": 3}, None],
)
def test_cleanup_threads_without_a_summary_is_a_service_error_not_a_guess(handler, service, result):
    service.results["cleanup_threads"] = result
    with pytest.raises(litellm.exceptions.APIError, match="bad_response"):
        handler.completion(
            model="grok",
            messages=[],
            model_response=_fresh_model_response(),
            optional_params={"action": "cleanup_threads"},
        )

# ── Auto-bootstrap + conventional-API backup ─────────────────────────


def test_bootstrap_routes_unwired_chat_site_generically(handler, monkeypatch):
    """A chat-kind registry site with NO litellm_model (e.g. deepseek)
    is callable as uc/<name> via the generic engine when auto-bootstrap
    is on (the default)."""
    from uc_browser.sites import generic as generic_mod

    monkeypatch.setenv("UC_AVAILABILITY_GATE", "0")
    monkeypatch.delenv("UC_AUTO_BOOTSTRAP", raising=False)
    fake = MagicMock()
    fake.send.return_value = {"response": "boot hello", "site": "deepseek",
                              "page_url": "https://chat.deepseek.com/"}
    with patch.object(generic_mod, "get_generic_client", return_value=fake):
        out = handler.completion(
            model="deepseek",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )
    assert out.choices[0].message.content == "boot hello"
    assert out.model == "uc/deepseek"


def test_bootstrap_off_rejects_unwired_site(handler, monkeypatch):
    monkeypatch.setenv("UC_AUTO_BOOTSTRAP", "0")
    with pytest.raises(litellm.exceptions.BadRequestError):
        handler.completion(
            model="deepseek",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )


def test_widget_sites_never_bootstrap(handler, monkeypatch):
    """Widget-kind sites reach human support staff — callability must be
    an explicit decision, so bootstrap skips them even when enabled."""
    import uc_browser.registry as reg_mod

    monkeypatch.delenv("UC_AUTO_BOOTSTRAP", raising=False)
    entry = reg_mod.SiteEntry(name="acme-widget", url="https://acme.example",
                              kind="widget")
    model, bootstrapped = reg_mod.advertised_model(entry)
    assert model is None and bootstrapped is False


def test_backup_model_used_when_site_down(handler, monkeypatch, tmp_path):
    """Availability gate blocks + backup configured → the request routes
    to the conventional-API model, labeled in hidden params."""
    import time as _time

    from uc_browser.health import STATUS_DOWN, HealthStore, ProbeResult

    monkeypatch.setenv("UC_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.delenv("UC_AVAILABILITY_GATE", raising=False)
    monkeypatch.setenv("UC_BACKUP_MODEL", "gpt-4o-mini")
    monkeypatch.delenv("UC_AUTO_BOOTSTRAP", raising=False)
    HealthStore().record(ProbeResult(
        site="deepseek", status=STATUS_DOWN, level_reached="none",
        latency_ms=1, at=_time.time()))

    backup_resp = _fresh_model_response()
    backup_resp.choices[0].message.content = "backup answer"
    with patch.object(litellm, "completion", return_value=backup_resp) as bc:
        out = handler.completion(
            model="deepseek",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )
    bc.assert_called_once()
    assert bc.call_args.kwargs["model"] == "gpt-4o-mini"
    assert out.choices[0].message.content == "backup answer"
    assert out._hidden_params["uc_backup_used"] == "gpt-4o-mini"
    assert "down" in out._hidden_params["uc_backup_reason"]


def test_no_backup_flag_disables_fallback(handler, monkeypatch, tmp_path):
    import time as _time

    from uc_browser.health import STATUS_DOWN, HealthStore, ProbeResult

    monkeypatch.setenv("UC_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.delenv("UC_AVAILABILITY_GATE", raising=False)
    monkeypatch.setenv("UC_BACKUP_MODEL", "gpt-4o-mini")
    monkeypatch.delenv("UC_AUTO_BOOTSTRAP", raising=False)
    HealthStore().record(ProbeResult(
        site="deepseek", status=STATUS_DOWN, level_reached="none",
        latency_ms=1, at=_time.time()))

    with pytest.raises(litellm.exceptions.ServiceUnavailableError):
        handler.completion(
            model="deepseek",
            messages=[{"role": "user", "content": "hi"}],
            model_response=_fresh_model_response(),
            optional_params={"extra_body": {"no_backup": True}},
        )


def test_local_probe_failure_does_not_override_service_grok(
    handler, service, monkeypatch, tmp_path,
):
    import time as _time

    from uc_browser.health import STATUS_LOGIN, HealthStore, ProbeResult

    monkeypatch.setenv("UC_HEALTH_DIR", str(tmp_path / "health"))
    monkeypatch.delenv("UC_AVAILABILITY_GATE", raising=False)
    monkeypatch.setenv("UC_BACKUP_MODEL", "gpt-4o-mini")
    HealthStore().record(ProbeResult(
        site="grok", status=STATUS_LOGIN, level_reached="none",
        latency_ms=1, at=_time.time()))
    with patch.object(litellm, "completion") as backup:
        out = _ask(handler)
    assert out.choices[0].message.content == service.send_result["response"]
    assert len(service.sends()) == 1
    backup.assert_not_called()


# ── Async path ───────────────────────────────────────────────────────


def test_acompletion_sends_through_the_service(handler, service):
    async def _go():
        return await handler.acompletion(
            model="grok",
            messages=[{"role": "user", "content": "async-hi"}],
            model_response=_fresh_model_response(),
            optional_params={},
        )

    out = asyncio.run(_go())
    assert out.choices[0].message.content == "hello back"
    assert service.sends()[0].kwargs["message"] == "async-hi"


# ── End-to-end via litellm.completion() ──────────────────────────────


def test_litellm_completion_dispatches_to_handler(handler, service):
    litellm.custom_provider_map = [{"provider": PROVIDER, "custom_handler": handler}]
    resp = litellm.completion(
        model="uc/grok",
        messages=[{"role": "user", "content": "ping"}],
    )
    assert resp.choices[0].message.content == "hello back"
    assert resp.model == "uc/grok"
    assert [c.op for c in service.calls] == ["send"]
    assert service.sends()[0].kwargs["message"] == "ping"
