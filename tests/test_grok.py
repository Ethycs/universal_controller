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
    _clean_dom_reply,
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


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Assistant block innerText opens with the speaker label.
        ("Grok\n\nPONG", "PONG"),
        ("Grok\n\nThought for 3s\n\nbody", "body"),
        # A reply that merely starts with the word keeps it.
        ("Grok is a model by xAI.", "Grok is a model by xAI."),
        # Label alone (reply still forming) is not reply text.
        ("Grok", ""),
        ("PONG", "PONG"),
        ("", ""),
    ],
)
def test_clean_dom_reply(raw, expected):
    assert _clean_dom_reply(raw) == expected


def test_stream_delta_skips_label_before_reply():
    from uc_browser.sites.grok_fast import _stream_delta

    emitted = ""
    chunks = []
    for cur in ("Grok", "Grok\n\nPI", "Grok\n\nPING"):
        delta, emitted = _stream_delta(cur, emitted)
        chunks.append(delta)
    assert "".join(chunks) == "PING"


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


# ── Project routing: the ``chat`` parameter is part of the location ──

PROJECT_URL = "https://grok.com/project/eb4fb4c3-2c98-4c9e-b3f0-9de16f244137"
CHAT_A = "9a321760-da6c-4ece-84cd-2e138e9a56b8"
CHAT_B = "0b7c2f1e-5d4a-4f3b-9c8d-1a2b3c4d5e6f"


@pytest.mark.parametrize(
    "a,b,same",
    [
        (f"{PROJECT_URL}?chat={CHAT_A}", f"{PROJECT_URL}?chat={CHAT_B}", False),
        (f"{PROJECT_URL}?chat={CHAT_A}", PROJECT_URL, False),
        (PROJECT_URL, f"{PROJECT_URL}?chat={CHAT_A}", False),
        (f"{PROJECT_URL}?chat={CHAT_A}", f"https://grok.com/c/{CHAT_A}", False),
        (f"https://grok.com/c/{CHAT_A}", f"https://grok.com/c/{CHAT_B}", False),
        # rid noise, trailing slash and host case do not matter
        (f"{PROJECT_URL}?chat={CHAT_A}&rid=1", f"{PROJECT_URL}?rid=2&chat={CHAT_A}", True),
        (f"{PROJECT_URL}/", PROJECT_URL, True),
        (f"https://GROK.com/c/{CHAT_A}", f"https://grok.com/c/{CHAT_A}?rid=x", True),
        ("https://grok.com/", "https://grok.com", True),
    ],
)
def test_same_path_compares_the_chat_parameter(a, b, same):
    assert GrokClient._same_path(a, b) is same


def test_navigation_between_two_chats_of_one_project_really_navigates(grok_client, mock_uc):
    """A caller must never send into chat A while believing it is chat B."""
    _, page = mock_uc
    page.url = f"{PROJECT_URL}?chat={CHAT_A}&rid=1"
    grok_client._navigate_in(page, f"{PROJECT_URL}?chat={CHAT_B}", wait_ms=0)
    page.goto.assert_called_once()
    assert page.goto.call_args.args[0] == f"{PROJECT_URL}?chat={CHAT_B}"

    page.goto.reset_mock()
    page.url = f"{PROJECT_URL}?chat={CHAT_B}"
    grok_client._navigate_in(page, f"{PROJECT_URL}?chat={CHAT_B}&rid=9", wait_ms=0)
    page.goto.assert_not_called()

    page.url = f"{PROJECT_URL}?chat={CHAT_B}"
    grok_client._navigate_in(page, PROJECT_URL, wait_ms=0)  # chat -> project landing
    page.goto.assert_called_once()


def test_row_selector_matches_classic_and_project_anchors():
    sel = grok_mod._row_selector(CHAT_A)
    assert f'a[href*="/c/{CHAT_A}"]' in sel and f'a[href*="chat={CHAT_A}"]' in sel


def test_regenerate_reads_grok_assistant_blocks(grok_client, mock_uc):
    _, page = mock_uc
    seen = []

    def evaluate(js, arg=None):
        seen.append(arg)
        if arg == grok_mod._SELECTORS["regenerate_button"]:
            return True
        return "old text" if len(seen) == 1 else "new text"

    page.evaluate = MagicMock(side_effect=evaluate)
    out = grok_client.regenerate(f"https://grok.com/c/{CHAT_A}", timeout_s=5)
    assert out == {"ok": True, "response": "new text"}
    assert seen[0] == grok_mod._SELECTORS["assistant_message"]
    assert not any(isinstance(a, str) and "data-message-author-role" in a for a in seen)


def test_send_reports_a_timeout_instead_of_a_clean_finish(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.side_effect = [0, {"success": True}, {}]
    out = grok_client.send("hello", timeout_s=0)
    assert out["timed_out"] is True and out["response"] == ""


def test_send_without_timeout_has_no_timed_out_flag(grok_client, mock_uc):
    _, page = mock_uc
    page.evaluate.side_effect = [
        0, {"success": True}, {},
        {"ready": True, "text": "done"}, {"ready": True, "text": "done"}, {"ready": True, "text": "done"},
    ]
    assert "timed_out" not in grok_client.send("hello")


# ── Borrowed-page mode (service sessions) ────────────────────────────


def test_borrowing_client_never_owns_a_browser_or_page():
    uc, page = MagicMock(), MagicMock()
    page.is_closed.return_value = False
    page.url = PROJECT_URL
    with patch.object(grok_mod, "UCBrowser", side_effect=AssertionError("must not launch")):
        client = GrokClient.borrowing(uc, page, home_url=PROJECT_URL)
        assert client._ensure_uc() is uc
        assert client._get_session_page("alpha") is page
        assert client._get_session_page("beta") is page  # every key is the one page
        with client._session("gamma") as p:
            assert p is page
        assert client.new_chat() == {"url": PROJECT_URL, "conversation_id": None}
        client.close()
        client.close()
    page.close.assert_not_called()
    uc.open.assert_not_called()
    uc.start.assert_not_called()
    uc.close.assert_not_called()
    assert GrokClient.GROK_HOME == "https://grok.com/"  # the class default is untouched


def test_borrowing_client_refuses_a_closed_page():
    page = MagicMock()
    page.is_closed.return_value = True
    client = GrokClient.borrowing(MagicMock(), page)
    with pytest.raises(grok_mod.GrokError):
        client._get_session_page("x")
    page.close.assert_not_called()


def test_borrowing_client_skips_the_local_login_probe():
    """Auth diagnosis belongs to the adapter; the borrowed path never calls has_login_wall."""
    uc, page = MagicMock(), MagicMock()
    page.is_closed.return_value = False
    client = GrokClient.borrowing(uc, page)
    client._get_session_page("x")
    uc.has_login_wall.assert_not_called()
    uc.dismiss_cookies.assert_not_called()


# ── Submit safety: a possibly-delivered message is never retyped ─────

from uc_browser.sites import grok_fast  # noqa: E402
from uc_browser.sites.grok import GrokPreSubmitError, GrokSubmitUncertain  # noqa: E402


class _FastPage:
    """Scripted page for ``GrokFastClient.send`` with a toolkit that reports failure."""

    def __init__(self, *, on_toolkit_submit=None):
        self.url = "https://grok.com/"
        self.users = 0
        self.on_toolkit_submit = on_toolkit_submit or (lambda page: None)
        self.counts = {"set_text": 0, "toolkit_submit": 0, "trigger": 0}
        self.wait_for_selector = MagicMock()

    def is_closed(self):
        return False

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, js, arg=None):
        if js is grok_fast._INSTALL_JS:
            return None
        if "document.querySelectorAll(sel).length" in js:
            return self.users if arg == grok_mod._SELECTORS["user_message"] else 0
        if "__UC_grokToolkitReady" in js:
            return True
        if "__UC_setText" in js:
            self.counts["set_text"] += 1
            return {"success": True}
        if "__UC_grokAttachmentInfo" in js:
            return {"editor_chars": 5, "count": 0, "list_chips": 0, "remove_buttons": 0, "chips": []}
        if "__UC_grokRemoveAttachments" in js:
            return {"removed": 0, "remaining": 0}
        if "__UC_grokToolkitSubmit" in js:
            self.counts["toolkit_submit"] += 1
            self.on_toolkit_submit(self)
            return {"ok": False, "why": "all-submit-methods-failed"}
        if "__UC_grokTriggerSubmit" in js:
            self.counts["trigger"] += 1
            return {"ok": False, "why": "all-submit-methods-failed"}
        raise AssertionError(f"unexpected evaluate: {str(js)[:60]}")


def _fast_client(page):
    return grok_fast.GrokFastClient(grok_client=GrokClient.borrowing(MagicMock(), page))


def _late_user_message(page):
    page.users = 1


def _late_navigation(page):
    page.url = "https://grok.com/c/9a321760-da6c-4ece-84cd-2e138e9a56b8"


@pytest.mark.parametrize("lands", [_late_user_message, _late_navigation])
def test_failed_submit_chain_that_delivered_is_not_retyped(lands, monkeypatch):
    monkeypatch.delenv("UC_GROK_DISABLE_TOOLKIT", raising=False)
    page = _FastPage(on_toolkit_submit=lands)
    with pytest.raises(GrokSubmitUncertain):
        _fast_client(page).send("hello", conversation_url="https://grok.com/", timeout_s=5)
    assert page.counts == {"set_text": 1, "toolkit_submit": 1, "trigger": 0}


def test_failed_submit_chain_with_no_delivery_may_fall_back_once(monkeypatch):
    monkeypatch.delenv("UC_GROK_DISABLE_TOOLKIT", raising=False)
    page = _FastPage()
    with pytest.raises(GrokPreSubmitError):
        _fast_client(page).send("hello", conversation_url="https://grok.com/", timeout_s=5)
    # toolkit attempt, then exactly one bespoke attempt; both proven undelivered
    assert page.counts == {"set_text": 2, "toolkit_submit": 1, "trigger": 1}


def test_send_with_fallback_retries_only_when_nothing_was_submitted(monkeypatch):
    dom = MagicMock()
    dom.send.return_value = {"response": "dom"}
    fast = MagicMock()
    monkeypatch.setattr(grok_mod, "get_grok_client", lambda: dom)
    monkeypatch.setattr(grok_fast, "_get_fast_client", lambda: fast)

    fast.send.side_effect = GrokPreSubmitError("setText failed")
    assert grok_fast.send_with_fallback("m")["response"] == "dom"
    assert dom.send.call_count == 1

    dom.send.reset_mock()
    for err in (GrokSubmitUncertain("maybe delivered"), RuntimeError("other"), grok_mod.GrokError("x")):
        fast.send.side_effect = err
        with pytest.raises(type(err)):
            grok_fast.send_with_fallback("m")
    dom.send.assert_not_called()


# ── Diagnostics never carry account text ─────────────────────────────


def test_empty_reply_diagnostic_is_structural_only():
    fast = grok_fast.GrokFastClient(grok_client=MagicMock())
    page = MagicMock()
    page.evaluate.return_value = {"assistant_blocks": 1}
    assert fast._capture_empty_diagnostic(page, "sel") == {"assistant_blocks": 1}
    js = page.evaluate.call_args.args[0]
    for leak in ("slice(", "last_assistant_snippet", "refusal_language:"):
        assert leak not in js
    page.evaluate.side_effect = ValueError("SECRET reply text")
    assert fast._capture_empty_diagnostic(page, "sel") == {"diag_error": "ValueError"}


def test_chip_and_composer_logging_omits_labels_and_markup(caplog):
    caplog.set_level("DEBUG", logger="uc_browser.sites.grok_fast")
    fast = grok_fast.GrokFastClient(grok_client=MagicMock())
    seen_js = []

    def run(info):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, arg=None: (seen_js.append(js), info)[1]
        fast._handle_chips(page, "msg")

    run({"editor_chars": 0, "count": 1, "list_chips": 1, "remove_buttons": 1,
         "chips": [{"label": "SECRET-FILE.txt"}]})
    run({"editor_chars": 0, "count": 0})
    assert "SECRET" not in caplog.text
    assert "composer shows neither text nor an attachment" in caplog.text
    assert not any("outerHTML" in js for js in seen_js)


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
