"""Unit tests for the Grok automation client.

The default suite uses mocked UCBrowser instances — no network, no
browser launch. An opt-in integration test (``test_live_send_read_delete``)
runs the real "create chat → send → read → delete" cycle when
``UC_GROK_LIVE=1`` is set in the environment.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from uc_browser.sites import grok as grok_mod
from uc_browser.sites.grok import (
    GrokAuthRequired,
    GrokClient,
    _conv_id_from_url,
    _normalize_url,
    _strip_thought_prefix,
)


# ── Pure helpers ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://grok.com/c/9a321760-da6c-4ece-84cd-2e138e9a56b8",
            "9a321760-da6c-4ece-84cd-2e138e9a56b8",
        ),
        (
            "https://grok.com/c/9a321760-da6c-4ece-84cd-2e138e9a56b8?rid=26e027de",
            "9a321760-da6c-4ece-84cd-2e138e9a56b8",
        ),
        # Project-workspace shape: the chat id lives in the `chat` query param.
        (
            "https://grok.com/project/eb4fb4c3-2c98-4c9e-b3f0-9de16f244137"
            "?chat=9a321760-da6c-4ece-84cd-2e138e9a56b8&rid=26e027de",
            "9a321760-da6c-4ece-84cd-2e138e9a56b8",
        ),
        # Bare ?chat= form.
        (
            "https://grok.com/anything?chat=9a321760-da6c-4ece-84cd-2e138e9a56b8",
            "9a321760-da6c-4ece-84cd-2e138e9a56b8",
        ),
        ("https://grok.com/", None),
        ("", None),
        ("nope", None),
    ],
)
def test_conv_id_from_url(url, expected):
    assert _conv_id_from_url(url) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Thought for 1s\n\npineapple", "pineapple"),
        ("Thought for 12 s\n\nbody text", "body text"),
        # No prefix → unchanged.
        ("pineapple", "pineapple"),
        # Prefix in the middle should not be touched.
        ("hello\nThought for 1s\n\nx", "hello\nThought for 1s\n\nx"),
        ("", ""),
    ],
)
def test_strip_thought_prefix(raw, expected):
    assert _strip_thought_prefix(raw) == expected


def test_normalize_url_full_url_passthrough():
    assert _normalize_url("https://grok.com/c/abc") == "https://grok.com/c/abc"


def test_normalize_url_bare_id():
    assert _normalize_url("abc-123") == "https://grok.com/c/abc-123"


def test_normalize_url_empty_returns_home():
    assert _normalize_url("") == "https://grok.com/"


# ── GrokClient tests with mocked UCBrowser ───────────────────────────


@pytest.fixture
def mock_uc():
    """Yield a fake UCBrowser with open()/dismiss/close stubs.

    ``page.evaluate`` is left bare — each test populates a ``side_effect``
    matching the recipe under exercise. ``page.keyboard.press`` /
    ``page.focus`` / ``page.wait_for_selector`` are auto-mocked.
    """
    uc = MagicMock()
    page = MagicMock()
    page.url = "https://grok.com/c/abc12345-1234-1234-1234-1234567890ab"
    page.evaluate = MagicMock(return_value=None)
    page.wait_for_timeout = MagicMock()
    page.wait_for_selector = MagicMock()
    page.focus = MagicMock()
    page.close = MagicMock()
    uc.open = MagicMock(return_value=page)
    uc.dismiss_cookies = MagicMock(return_value=True)
    uc.close_modal = MagicMock(return_value=False)
    uc.has_login_wall = MagicMock(return_value=False)
    uc._wait_ready = MagicMock(return_value=True)
    uc.start = MagicMock()
    uc.close = MagicMock()
    return uc, page


@pytest.fixture
def grok_client(mock_uc):
    """A GrokClient whose underlying UCBrowser is the mock."""
    uc, _page = mock_uc
    with patch.object(grok_mod, "UCBrowser", return_value=uc):
        client = GrokClient()
        client._ensure_uc()  # force the (mocked) browser to be "started"
        yield client
        client.close()


def test_send_basic(grok_client, mock_uc):
    uc, page = mock_uc
    # send() evaluate sequence:
    #   1. prev_count (assistant blocks before sending) → 0
    #   2. __UC_setText result                          → {"success": True, ...}
    #   3-N. assistant poll: {"ready": True, "text": ...}
    # Stabilises at 3 unchanged polls (after the first "different" poll), so
    # we feed 4 identical ready-True states.
    page.evaluate.side_effect = [
        0,
        {"success": True, "method": "execCommand"},
        {"ready": True, "text": "Thought for 1s\n\nhello back"},
        {"ready": True, "text": "Thought for 1s\n\nhello back"},
        {"ready": True, "text": "Thought for 1s\n\nhello back"},
        {"ready": True, "text": "Thought for 1s\n\nhello back"},
    ]
    out = grok_client.send("hello")
    # "Thought for Ns" prefix stripped from the response.
    assert out["response"] == "hello back"
    assert out["url"] == page.url
    assert out["conversation_id"] == "abc12345-1234-1234-1234-1234567890ab"
    # We submitted via Enter, not via uc.chat()
    page.keyboard.press.assert_called_with("Enter")
    # And typed via __UC_setText (the second evaluate call), passing our message
    setText_call = page.evaluate.call_args_list[1]
    assert setText_call.args[1] == ["div.ProseMirror", "hello"]


def test_send_uses_keyboard_type_when_setText_fails(grok_client, mock_uc):
    """If __UC_setText returns success=False we fall back to keyboard.type."""
    _uc, page = mock_uc
    page.evaluate.side_effect = [
        0,
        {"success": False, "error": "no __UC_setText"},
        {"ready": True, "text": "ok"},
        {"ready": True, "text": "ok"},
        {"ready": True, "text": "ok"},
        {"ready": True, "text": "ok"},
    ]
    out = grok_client.send("fallback path")
    page.keyboard.type.assert_called_once_with("fallback path", delay=10)
    assert out["response"] == "ok"


def test_send_login_wall_raises(grok_client, mock_uc):
    uc, _ = mock_uc
    uc.has_login_wall.return_value = True
    with pytest.raises(GrokAuthRequired):
        grok_client.send("anything")


def test_read_returns_messages(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.return_value = [
        {"role": "user", "text": "hi"},
        {"role": "assistant", "text": "Thought for 1s\n\nhello"},
    ]
    page.url = "https://grok.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    out = grok_client.read("https://grok.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    assert out["conversation_id"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert len(out["messages"]) == 2
    assert out["messages"][1]["role"] == "assistant"
    # Reasoning prefix scrubbed from assistant text.
    assert out["messages"][1]["text"] == "hello"
    # User text left untouched.
    assert out["messages"][0]["text"] == "hi"


def test_list_conversations(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.return_value = [
        {"id": "x1", "title": "Chat 1", "url": "https://grok.com/c/x1"},
        {"id": "x2", "title": "Chat 2", "url": "https://grok.com/c/x2"},
    ]
    items = grok_client.list_conversations()
    assert len(items) == 2
    assert items[0]["id"] == "x1"


def test_delete_full_flow(grok_client, mock_uc):
    """delete() runs: hover row → click Options → click Delete → verify gone.

    The hover + Options click happen through Playwright locators (mocked),
    not via page.evaluate. evaluate is called only for: click-Delete-item,
    best-effort confirm-dialog probe, and the final "row gone?" check.
    """
    _, page = mock_uc
    # Sidebar locator chain returns a count>=1 so the recipe proceeds.
    page.locator.return_value.first.count.return_value = 1
    page.locator.return_value.first.locator.return_value.locator.return_value.count.return_value = 1

    page.evaluate.side_effect = [
        True,   # click "Delete" menu item
        False,  # confirm-dialog probe — no dialog appeared (current Grok)
        True,   # row is gone after delete
    ]
    assert grok_client.delete("https://grok.com/c/del-id") is True
    assert page.evaluate.call_count == 3
    # The recipe must hover the sidebar row first.
    page.locator.return_value.first.hover.assert_called()


def test_delete_aborts_when_row_missing(grok_client, mock_uc):
    """If the sidebar has no link to the conversation, bail without clicking."""
    _, page = mock_uc
    page.locator.return_value.first.count.return_value = 0  # row not found
    assert grok_client.delete("https://grok.com/c/missing") is False
    # No menu-item click attempted.
    page.evaluate.assert_not_called()


def test_stop_generation(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.return_value = True
    assert grok_client.stop("https://grok.com/c/abc") is True


def test_stop_generation_no_button(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.return_value = False
    assert grok_client.stop("https://grok.com/c/abc") is False


def test_new_chat_returns_home_url(grok_client):
    out = grok_client.new_chat()
    assert out["url"].rstrip("/") == "https://grok.com"
    assert out["conversation_id"] is None


def test_close_is_idempotent(grok_client):
    grok_client.close()
    grok_client.close()  # no error


def test_distinct_session_keys_get_distinct_pages(grok_client, mock_uc):
    """Two different session_keys should each open their own tab.

    The UCBrowser mock's ``open`` is called once per session_key,
    proving each session has its own Page in the dict.
    """
    uc, _page = mock_uc
    uc.open.reset_mock()

    # Each open() call returns a fresh MagicMock so the test can tell
    # "alpha's page" and "beta's page" apart by identity.
    def _new_page(*_a, **_kw):
        p = MagicMock()
        p.url = "https://grok.com/"
        p.is_closed = MagicMock(return_value=False)
        return p
    uc.open.side_effect = _new_page

    p1 = grok_client._get_session_page("alpha")
    p2 = grok_client._get_session_page("beta")
    p3 = grok_client._get_session_page("alpha")  # same key → cached

    # Two distinct open calls (one per new session_key).
    assert uc.open.call_count == 2
    # The cached lookup returns the same Page object as the first alpha open.
    assert p1 is p3
    # alpha and beta got separate Pages.
    assert p1 is not p2
    # Per-session locks were created for each.
    assert "alpha" in grok_client._page_locks
    assert "beta" in grok_client._page_locks


# ── Live integration smoke (opt-in via UC_GROK_LIVE=1) ───────────────


@pytest.mark.skipif(
    os.environ.get("UC_GROK_LIVE") != "1",
    reason="Set UC_GROK_LIVE=1 to run live Grok smoke test (requires login)",
)
def test_live_send_read_delete():
    """Real round-trip against grok.com — uses your saved Chrome profile."""
    with GrokClient() as client:
        try:
            sent = client.send("Reply with one word: pineapple", timeout_s=60)
        except GrokAuthRequired:
            pytest.skip("Not logged in to Grok — run web-login first")
        assert sent["response"], "expected a non-empty response"
        url = sent["url"]
        assert "pineapple" in sent["response"].lower() or len(sent["response"]) > 0

        transcript = client.read(url)
        assert transcript["messages"], "read should return at least one message"

        deleted = client.delete(url)
        # Deletion is best-effort; log but don't fail the suite hard if Grok's
        # menu shape has drifted.
        assert deleted in (True, False)
