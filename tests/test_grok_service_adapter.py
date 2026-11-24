"""Deterministic tests for the service-session Grok adapter.

No browser, network or Playwright: the page is a scripted fake and the inner
Grok clients are recorded fakes. They pin the adapter's own guarantees:
borrowed ownership, project routing, structural auth/deviation diagnosis,
folder membership, never re-sending a possibly-submitted message, streaming
and timeout semantics.
"""

from __future__ import annotations

import re
import threading
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from uc_browser.service.errors import ServiceError
from uc_browser.sites import grok as grok_mod
from uc_browser.sites import grok_service as gs
from uc_browser.sites.grok import (
    _CLICK_STOP_JS,
    GrokPreSubmitError,
    GrokSubmitUncertain,
)
from uc_browser.sites.grok_service import GrokServiceError, GrokSessionAdapter

PROJECT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
FOLDER_URL = f"https://grok.com/project/{PROJECT}"
CHAT_A = "9a321760-da6c-4ece-84cd-2e138e9a56b8"
CHAT_B = "0b7c2f1e-5d4a-4f3b-9c8d-1a2b3c4d5e6f"
CHAT_C = "7f6e5d4c-3b2a-4190-8f7e-6d5c4b3a2910"
FOREIGN = "11111111-2222-4333-8444-555555555555"


def chat_url(cid: str) -> str:
    return f"{FOLDER_URL}?chat={cid}"


# ── fakes ────────────────────────────────────────────────────────────────


class FakePage:
    """A page whose structural probe is computed from a scripted ``dom``."""

    def __init__(self, url: str = "about:blank", **dom):
        self.url = url
        self.closed = False
        self.dom = {
            "composer_visible": True, "composer_enabled": True,
            "auth_links": 0, "auth_fields": 0, "auth_dialog": False,
            "list": [], "stop": False,
        }
        self.dom.update(dom)
        self.redirects: dict[str, str] = {}
        self.gotos: list[str] = []
        self.list_args: list = []
        self.close = MagicMock(name="page.close")  # must never be called
        self.keyboard = MagicMock()

    def is_closed(self):
        return self.closed

    def goto(self, url, **_kw):
        self.gotos.append(url)
        self.url = self.redirects.get(url, url)

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, js, arg=None):
        if js is gs._SURFACE_JS:
            path = urlsplit(self.url).path
            d = self.dom
            return {
                "url": self.url,
                "composer": {"present": d["composer_visible"], "visible": d["composer_visible"],
                             "enabled": d["composer_enabled"]},
                "auth": {"route": bool(re.match(r"^/(sign-?in|log-?in|login|auth)(/|$)", path)),
                         "links": d["auth_links"], "fields": d["auth_fields"],
                         "dialog": d["auth_dialog"]},
            }
        if js is gs._PROJECT_GET_JS:
            if arg.startswith("/rest/workspaces/"):
                return self.dom.get("project_response", {
                    "status": 200, "data": {"workspaceId": PROJECT, "name": "NSFW"},
                })
            self.list_args.append(arg)
            if "list_response" in self.dom:
                return self.dom["list_response"]
            token = parse_qs(urlsplit(arg).query).get("pageToken", [""])[0]
            if "pages" in self.dom:
                return {"status": 200, "data": self.dom["pages"][token]}
            return {"status": 200, "data": {"conversations": [
                {"conversationId": row["id"], "title": row["title"],
                 "workspaceId": row.get("workspaceId", PROJECT)}
                for row in self.dom["list"]
            ]}}
        if js is _CLICK_STOP_JS:
            return self.dom["stop"]
        raise AssertionError(f"unexpected page.evaluate: {str(js)[:60]}")


class FakeRuntime:
    def __init__(self):
        self.uc = MagicMock(name="receiver")
        self.engine_pages: list = []
        self.receivers = 0

    def receiver(self):
        self.receivers += 1
        return self.uc

    def ensure_engine(self, page):
        self.engine_pages.append(page)
        return True


class FakeFast:
    def __init__(self):
        self.calls: list[dict] = []
        self.script = lambda **kw: {"response": "ok", "url": kw["conversation_url"],
                                    "conversation_id": None}

    def send(self, message, *, conversation_url=None, session_key=None, timeout_s=120, on_delta=None):
        call = dict(message=message, conversation_url=conversation_url,
                    session_key=session_key, timeout_s=timeout_s, on_delta=on_delta)
        self.calls.append(call)
        return self.script(**call)


class FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []
        self.send_result = {"response": "dom", "url": FOLDER_URL, "conversation_id": None}
        self.delete_result = True
        self.rename_result = True
        self.archive_result = True
        self.regenerate_result = {"ok": True, "response": "again"}
        self.read_result = None

    def send(self, message, **kw):
        self.calls.append(("send", message, kw))
        return dict(self.send_result)

    def read(self, url):
        self.calls.append(("read", url))
        return self.read_result or {"url": url, "conversation_id": grok_mod._conv_id_from_url(url),
                                    "messages": [{"role": "user", "text": "hi"}]}

    def delete(self, cid):
        self.calls.append(("delete", cid))
        return self.delete_result

    def rename(self, cid, title):
        self.calls.append(("rename", cid, title))
        return self.rename_result

    def archive(self, cid):
        self.calls.append(("archive", cid))
        return self.archive_result

    def regenerate(self, url, timeout_s=60):
        self.calls.append(("regenerate", url, timeout_s))
        return dict(self.regenerate_result)

    def get_models(self):
        self.calls.append(("get_models",))
        return ["grok-4", "grok-3"]

    def switch_model(self, name):
        self.calls.append(("switch_model", name))
        return True

    def list_conversations(self):
        self.calls.append(("list_conversations",))
        return [{"id": "x", "title": "t", "url": "https://grok.com/c/x"}]

    def names(self):
        return [c[0] for c in self.calls]


def make(page=None, *, scoped=True, **dom):
    page = page or FakePage(**dom)
    runtime = FakeRuntime()
    kwargs = {"folder": "NSFW", "folder_url": FOLDER_URL} if scoped else {}
    adapter = GrokSessionAdapter(runtime, page, **kwargs)
    adapter._fast = FakeFast()
    adapter._client = FakeClient()
    return adapter, page, runtime


def item(cid, title="t"):
    return {"id": cid, "href": chat_url(cid), "title": title}


# ── operations collection and argument validation ────────────────────────




@pytest.mark.parametrize("op", ["_op_send", "send_with_fallback", "close", "url", "execute", "nope", ""])
def test_unknown_or_private_operation_is_refused(op):
    adapter, page, _ = make()
    with pytest.raises(ServiceError) as exc:
        adapter.execute(op, {})
    assert (exc.value.code, exc.value.status) == ("no_such_op", 404)
    assert page.gotos == []


@pytest.mark.parametrize(
    "op,kwargs",
    [
        ("send", {}),
        ("send", {"message": "  "}),
        ("send", {"message": "hi", "extra": 1}),
        ("send", {"message": "hi", "timeout_s": 0}),
        ("send", {"message": "hi", "timeout_s": True}),
        ("send", {"message": "hi", "timeout_s": float("nan")}),
        ("send", {"message": "hi", "wait_for_response": "yes"}),
        ("send", {"message": "hi", "on_delta": print}),
        ("read", {}),
        ("rename", {"conversation_url": CHAT_A}),
        ("rename", {"conversation_url": CHAT_A, "new_title": "x" * 500}),
        ("switch_model", {"model_name": ""}),
        ("cleanup_threads", {"keep_recent": -1}),
        ("cleanup_threads", {"keep_recent": True}),
        ("cleanup_threads", {"keep_recent": "1"}),
        ("list_conversations", {"x": 1}),
    ],
)
def test_bad_arguments_are_rejected_before_touching_the_page(op, kwargs):
    adapter, page, _ = make()
    with pytest.raises(ServiceError) as exc:
        adapter.execute(op, kwargs)
    assert (exc.value.code, exc.value.status) == ("bad_arguments", 400)
    assert page.gotos == [] and adapter._fast.calls == [] and adapter._client.calls == []


@pytest.mark.parametrize("value", ["not-a-uuid", "https://example.com/c/" + CHAT_A, "https://grok.com/"])
def test_conversation_argument_must_name_one_grok_conversation(value):
    adapter, page, _ = make()
    with pytest.raises(ServiceError) as exc:
        adapter.execute("read", {"conversation_url": value})
    assert exc.value.code == "bad_conversation"
    assert page.gotos == []


def test_folder_configuration_is_validated():
    rt = FakeRuntime()
    with pytest.raises(ValueError):
        GrokSessionAdapter(rt, FakePage(), folder="NSFW")
    with pytest.raises(ValueError):
        GrokSessionAdapter(rt, FakePage(), folder="", folder_url=FOLDER_URL)
    with pytest.raises(ValueError):
        GrokSessionAdapter(rt, FakePage(), folder="x", folder_url="http://grok.com/project/a")
    with pytest.raises(ValueError):
        GrokSessionAdapter(rt, FakePage(), folder="x", folder_url="https://grok.com/")


# ── borrowed ownership ───────────────────────────────────────────────────


def test_adapter_borrows_the_page_and_never_owns_a_browser():
    page = FakePage(url=chat_url(CHAT_A))
    runtime = FakeRuntime()
    with patch.object(grok_mod, "UCBrowser", side_effect=AssertionError("adapter must not launch a browser")):
        adapter = GrokSessionAdapter(runtime, page, folder="NSFW", folder_url=FOLDER_URL)
    assert runtime.receivers == 1
    assert adapter.url == page.url
    assert adapter._client._borrowed_page is page
    assert adapter._client._uc is runtime.uc
    adapter._fast = FakeFast()
    adapter._client = FakeClient()
    adapter._members[CHAT_A] = chat_url(CHAT_A)
    page.dom["list"] = [item(CHAT_A)]
    adapter.execute("send", {"message": "hi", "conversation_url": chat_url(CHAT_A)})
    adapter.execute("read", {"conversation_url": chat_url(CHAT_A)})
    adapter.execute("list_conversations", {})
    adapter.execute("new_chat", {})
    page.close.assert_not_called()
    runtime.uc.start.assert_not_called()
    runtime.uc.close.assert_not_called()
    runtime.uc.open.assert_not_called()
    assert runtime.engine_pages and all(p is page for p in runtime.engine_pages)




def test_closed_page_is_reported_not_driven():
    adapter, page, _ = make()
    page.closed = True
    with pytest.raises(ServiceError) as exc:
        adapter.execute("new_chat", {})
    assert exc.value.code == "session_page_closed"
    assert page.gotos == []


# ── project routing ──────────────────────────────────────────────────────


def test_new_conversation_starts_in_the_project_and_keeps_its_url():
    adapter, page, runtime = make()

    def script(**kw):
        page.url = chat_url(CHAT_A) + "&rid=abc"  # Grok moved the page into the chat
        return {"response": "pong", "url": f"https://grok.com/c/{CHAT_A}", "conversation_id": CHAT_A}

    adapter._fast.script = script
    out = adapter.execute("send", {"message": "ping"})
    assert page.gotos == [FOLDER_URL]  # never the account home
    call = adapter._fast.calls[0]
    assert call["conversation_url"] == FOLDER_URL  # the inner client has nothing to navigate to
    assert call["timeout_s"] == gs.DEFAULT_SEND_TIMEOUT_S
    assert out["response"] == "pong"
    assert out["url"] == chat_url(CHAT_A) + "&rid=abc"  # the actual project URL, not /c/<id>
    assert out["conversation_id"] == CHAT_A
    assert out["folder"] == "NSFW" and out["folder_url"] == FOLDER_URL
    assert out["folder_verified"] is True
    assert adapter._members[CHAT_A] == chat_url(CHAT_A)
    assert runtime.engine_pages  # engine placed before typing


def test_canonical_chat_url_requires_backend_project_membership():
    adapter, page, _ = make(list=[item(CHAT_A)])

    def script(**kw):
        page.url = f"https://grok.com/c/{CHAT_A}"
        return {"response": "pong", "url": page.url, "conversation_id": CHAT_A}

    adapter._fast.script = script
    out = adapter.execute("send", {"message": "ping"})
    assert out["url"] == chat_url(CHAT_A)
    assert out["page_url"] == f"https://grok.com/c/{CHAT_A}"
    assert out["folder_verified"] is True


def test_switching_between_chats_of_one_project_navigates_each_time():
    adapter, page, _ = make(url=chat_url(CHAT_A))
    page.dom["list"] = [item(CHAT_A), item(CHAT_B)]
    adapter.execute("send", {"message": "to b", "conversation_url": chat_url(CHAT_B)})
    adapter.execute("send", {"message": "to a", "conversation_url": chat_url(CHAT_A)})
    assert [c["conversation_url"] for c in adapter._fast.calls] == [chat_url(CHAT_B), chat_url(CHAT_A)]


def test_new_chat_leaves_a_conversation_for_the_project_landing():
    adapter, page, _ = make(url=chat_url(CHAT_A))
    out = adapter.execute("new_chat", {})
    assert page.gotos == [FOLDER_URL]
    assert out["url"] == FOLDER_URL and out["conversation_id"] is None
    assert out["folder"] == "NSFW"


def test_unscoped_adapter_uses_the_account_home():
    adapter, page, _ = make(scoped=False)
    out = adapter.execute("new_chat", {})
    assert page.gotos == ["https://grok.com/"]
    assert "folder" not in out and adapter.folder_url is None


def test_redirect_out_of_the_project_stops_before_anything_is_sent():
    adapter, page, _ = make()
    page.redirects[FOLDER_URL] = "https://grok.com/"
    adapter.hydrate_s = 1000  # exercise the stable-mismatch exit, not the deadline
    with pytest.raises(ServiceError) as exc:
        adapter.execute("send", {"message": "secret"})
    assert (exc.value.code, exc.value.status) == ("page_deviation", 502)
    assert adapter._fast.calls == [] and adapter._client.calls == []


def test_landing_that_resumes_a_chat_is_not_a_new_chat_surface():
    adapter, page, _ = make()
    page.redirects[FOLDER_URL] = chat_url(CHAT_A)
    adapter.hydrate_s = 1000
    with pytest.raises(ServiceError) as exc:
        adapter.execute("send", {"message": "x"})
    assert exc.value.code == "page_deviation"
    assert adapter._fast.calls == []


def test_navigation_failure_is_a_service_error_not_a_send():
    from playwright.sync_api import Error as PlaywrightError

    adapter, page, _ = make()

    def boom(url, **_kw):
        raise PlaywrightError("net::ERR_FAILED")

    page.goto = boom
    with pytest.raises(ServiceError) as exc:
        adapter.execute("send", {"message": "x"})
    assert exc.value.code == "navigation_failed"
    assert adapter._fast.calls == []


# ── structural diagnosis ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "state,identity,expected",
    [
        ({"composer": {"visible": True, "enabled": True}, "auth": {}}, True, "ready"),
        ({"composer": {"visible": True, "enabled": True}, "auth": {}}, False, "page_deviation"),
        ({"composer": {"visible": True, "enabled": False}, "auth": {}}, True, "page_deviation"),
        ({"composer": {"visible": False, "enabled": False}, "auth": {}}, True, "page_deviation"),
        ({"composer": {"visible": False}, "auth": {"links": 1}}, True, "auth_required"),
        ({"composer": {"visible": False}, "auth": {"fields": 1}}, True, "auth_required"),
        ({"composer": {"visible": True, "enabled": True}, "auth": {"route": True}}, True, "auth_required"),
        ({"composer": {"visible": True, "enabled": True}, "auth": {"dialog": True}}, True, "auth_required"),
        ({"composer": {"visible": True, "enabled": True}, "auth": {"links": 1}}, False, "auth_required"),
        # A sign-in link next to a working composer on the right page is not a wall.
        ({"composer": {"visible": True, "enabled": True}, "auth": {"links": 1}}, True, "ready"),
        ({}, False, "page_deviation"),
    ],
)
def test_classify_surface(state, identity, expected):
    assert gs.classify_surface(state, identity_ok=identity) == expected


@pytest.mark.parametrize(
    "dom,url,code,status",
    [
        ({"composer_visible": False, "auth_links": 2}, None, "auth_required", 401),
        ({"composer_visible": False, "auth_fields": 1}, None, "auth_required", 401),
        ({"composer_visible": False}, "https://grok.com/sign-in?redirect=/", "auth_required", 401),
        ({"auth_dialog": True, "auth_links": 1}, None, "auth_required", 401),
        ({"composer_visible": False}, None, "page_deviation", 502),
        ({"composer_enabled": False}, None, "page_deviation", 502),
    ],
)
def test_deviation_is_classified_structurally_and_nothing_is_sent(dom, url, code, status):
    adapter, page, _ = make(**dom)
    if url:
        page.redirects[FOLDER_URL] = url
    adapter.hydrate_s = 0.0  # expire at once: the deadline must raise, not fall through
    with pytest.raises(ServiceError) as exc:
        adapter.execute("send", {"message": "private words"})
    assert (exc.value.code, exc.value.status) == (code, status)
    assert "private words" not in exc.value.detail
    assert adapter._fast.calls == [] and adapter._client.calls == []


def test_signed_out_redirect_home_with_composer_is_auth_required():
    adapter, page, _ = make(auth_links=1)
    page.redirects[FOLDER_URL] = "https://grok.com/"
    adapter.hydrate_s = 1000
    with pytest.raises(ServiceError) as exc:
        adapter.execute("send", {"message": "x"})
    assert exc.value.code == "auth_required"
    assert adapter._fast.calls == []


def test_auth_error_names_a_path_only():
    adapter, page, _ = make(composer_visible=False, auth_links=1)
    page.redirects[FOLDER_URL] = "https://grok.com/sign-in?token=SECRET#frag"
    adapter.hydrate_s = 0.0
    with pytest.raises(GrokServiceError) as exc:
        adapter.execute("new_chat", {})
    assert "SECRET" not in exc.value.detail and "SECRET" not in repr(exc.value.data)
    assert exc.value.data["path"] == "/sign-in"


# ── folder membership ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "op,kwargs",
    [
        ("send", {"message": "hi", "conversation_url": f"https://grok.com/c/{FOREIGN}"}),
        ("read", {"conversation_url": f"https://grok.com/c/{FOREIGN}"}),
        ("regenerate", {"conversation_url": f"https://grok.com/c/{FOREIGN}"}),
        ("stop", {"conversation_url": f"https://grok.com/c/{FOREIGN}"}),
        ("delete", {"conversation_url": f"https://grok.com/c/{FOREIGN}"}),
        ("rename", {"conversation_url": FOREIGN, "new_title": "t"}),
        ("archive", {"conversation_url": f"https://grok.com/c/{FOREIGN}"}),
        # A project-shaped URL is not proof of membership either.
        ("read", {"conversation_url": chat_url(FOREIGN)}),
    ],
)
def test_conversation_outside_the_folder_is_refused(op, kwargs):
    adapter, page, _ = make()
    page.dom["list"] = [item(CHAT_A)]
    with pytest.raises(ServiceError) as exc:
        adapter.execute(op, kwargs)
    assert (exc.value.code, exc.value.status) == ("folder_mismatch", 409)
    assert adapter._fast.calls == []
    assert adapter._client.calls == []  # no send/read/delete/rename/archive/regenerate
    assert not page.dom["stop"]
    assert FOREIGN not in page.gotos and all(FOREIGN not in g for g in page.gotos)


def test_member_is_verified_from_the_project_list_then_navigated_to():
    adapter, page, _ = make()
    page.dom["list"] = [item(CHAT_B), item(CHAT_A)]
    out = adapter.execute("read", {"conversation_url": f"https://grok.com/c/{CHAT_A}"})
    assert page.gotos == [FOLDER_URL, chat_url(CHAT_A)]  # list the project, then open the chat
    assert out["conversation_id"] == CHAT_A and out["url"] == chat_url(CHAT_A)
    assert adapter._client.names() == ["read"]


def test_listing_returns_only_verified_project_conversations():
    adapter, page, _ = make()
    page.dom["list"] = [item(CHAT_A, "first"), item(CHAT_B, "second")]
    out = adapter.execute("list_conversations", {})
    assert [c["id"] for c in out] == [CHAT_A, CHAT_B]
    assert out[0]["url"] == chat_url(CHAT_A)
    assert parse_qs(urlsplit(page.list_args[0]).query)["workspaceId"] == [PROJECT]
    assert adapter._client.calls == []


def test_listing_follows_project_pagination_without_duplicate_rows():
    adapter, page, _ = make()
    def row(cid):
        return {"conversationId": cid, "title": "test", "workspaceId": PROJECT}
    page.dom["pages"] = {
        "": {"conversations": [row(CHAT_A)], "nextPageToken": "next/+?"},
        "next/+?": {"conversations": [row(CHAT_A), row(CHAT_B)]},
    }
    out = adapter.execute("list_conversations", {})
    assert [c["id"] for c in out] == [CHAT_A, CHAT_B]
    assert parse_qs(urlsplit(page.list_args[-1]).query) == {
        "pageSize": ["60"], "workspaceId": [PROJECT], "pageToken": ["next/+?"],
    }


@pytest.mark.parametrize("payload,code", [
    ({"conversations": [{"conversationId": CHAT_A, "workspaceId": "other"}]}, "folder_mismatch"),
    ({}, "page_deviation"),
    ({"conversations": [], "nextPageToken": "repeat"}, "page_deviation"),
])
def test_invalid_project_listing_is_not_silently_returned(payload, code):
    adapter, page, _ = make(list_response={"status": 200, "data": payload})
    with pytest.raises(ServiceError) as exc:
        adapter.execute("list_conversations", {})
    assert exc.value.code == code


@pytest.mark.parametrize("data", [
    {"workspaceId": PROJECT, "name": "another project"},
    {"workspaceId": "different", "name": "NSFW"},
])
def test_project_identity_mismatch_prevents_a_send(data):
    adapter, page, _ = make(project_response={"status": 200, "data": data})
    with pytest.raises(ServiceError) as exc:
        adapter.execute("send", {"message": "never send"})
    assert exc.value.code == "folder_mismatch"
    assert adapter._fast.calls == [] and adapter._client.calls == []


def test_delete_always_verifies_against_a_fresh_listing():
    adapter, page, _ = make()
    adapter._members[CHAT_A] = chat_url(CHAT_A)  # cached from an earlier send
    page.dom["list"] = []  # but it is no longer listed
    with pytest.raises(ServiceError) as exc:
        adapter.execute("delete", {"conversation_url": chat_url(CHAT_A)})
    assert exc.value.code == "folder_mismatch"
    assert adapter._client.calls == []

    page.dom["list"] = [item(CHAT_A)]
    out = adapter.execute("delete", {"conversation_url": chat_url(CHAT_A)})
    assert adapter._client.calls == [("delete", CHAT_A)]  # exact id, once
    assert out is True
    assert CHAT_A not in adapter._members


def test_failed_row_actions_close_the_popover_they_opened():
    adapter, page, _ = make()
    page.dom["list"] = [item(CHAT_A)]
    adapter._client.archive_result = False
    adapter._client.rename_result = False
    assert adapter.execute("archive", {"conversation_url": chat_url(CHAT_A)}) is False
    assert adapter.execute("rename", {"conversation_url": chat_url(CHAT_A), "new_title": " T "}) is False
    assert page.keyboard.press.call_count == 2
    assert ("rename", CHAT_A, "T") in adapter._client.calls


def test_cleanup_deletes_only_older_rows_of_the_project_list():
    adapter, page, _ = make()
    page.dom["list"] = [item(CHAT_A), item(CHAT_B), item(CHAT_C)]
    out = adapter.execute("cleanup_threads", {"keep_recent": 1})
    assert adapter._client.calls == [("delete", CHAT_B), ("delete", CHAT_C)]
    assert out == {
        "summary": "cleanup_threads: kept 1, deleted 2, failed 0, total_seen 3",
        "deleted_ids": [CHAT_B, CHAT_C],
    }
    assert page.gotos == [FOLDER_URL]  # one listing, not one per delete


def test_cleanup_counts_failures_without_stopping():
    adapter, page, _ = make()
    page.dom["list"] = [item(CHAT_A), item(CHAT_B), item(CHAT_C)]
    results = iter([False, True, True])
    adapter._client.delete = lambda cid: next(results)
    out = adapter.execute("cleanup_threads", {"keep_recent": 0})
    assert out == {
        "summary": "cleanup_threads: kept 0, deleted 2, failed 1, total_seen 3",
        "deleted_ids": [CHAT_B, CHAT_C],  # only the successful deletes
    }


def test_cleanup_without_a_folder_refuses_to_enumerate_the_account():
    adapter, page, _ = make(scoped=False)
    with pytest.raises(ServiceError) as exc:
        adapter.execute("cleanup_threads", {"keep_recent": 1})
    assert exc.value.code == "folder_required"
    assert page.gotos == [] and adapter._client.calls == []


# ── submit safety ────────────────────────────────────────────────────────


def test_proven_pre_submit_failure_falls_back_to_the_dom_path_exactly_once():
    adapter, page, _ = make()

    def script(**kw):
        raise GrokPreSubmitError("setText failed")

    adapter._fast.script = script
    adapter._client.send_result = {"response": "dom reply", "url": FOLDER_URL, "conversation_id": None}
    out = adapter.execute("send", {"message": "hello", "timeout_s": 42})
    assert len(adapter._fast.calls) == 1
    sends = [c for c in adapter._client.calls if c[0] == "send"]
    assert len(sends) == 1
    assert sends[0][1] == "hello"
    assert sends[0][2]["conversation_url"] == FOLDER_URL and sends[0][2]["timeout_s"] == 42
    assert out["response"] == "dom reply"


def test_uncertain_submit_is_never_resent():
    adapter, page, _ = make()
    page.url = FOLDER_URL

    def script(**kw):
        raise GrokSubmitUncertain("trigger failed, but the message appears to have been delivered")

    adapter._fast.script = script
    with pytest.raises(GrokServiceError) as exc:
        adapter.execute("send", {"message": "hello"})
    assert exc.value.code == "submit_uncertain"
    assert adapter._client.names() == []  # no DOM re-send
    assert len(adapter._fast.calls) == 1


@pytest.mark.parametrize("error", [RuntimeError("anything else"), ServiceError("busy", "x", 503),
                                   GrokPreSubmitError.__mro__[1]("generic grok error")])
def test_other_errors_never_trigger_a_second_submit(error):
    adapter, page, _ = make()

    def script(**kw):
        raise error

    adapter._fast.script = script
    with pytest.raises(type(error)):
        adapter.execute("send", {"message": "hello"})
    assert adapter._client.names() == []
    assert len(adapter._fast.calls) == 1


def test_blind_send_skips_the_fast_path_and_submits_once():
    adapter, page, _ = make()
    adapter._client.send_result = {"response": "", "url": chat_url(CHAT_A), "conversation_id": CHAT_A}
    out = adapter.execute("send", {"message": "fire", "wait_for_response": False})
    assert adapter._fast.calls == []
    assert [c[0] for c in adapter._client.calls] == ["send"]
    assert adapter._client.calls[0][2]["wait_for_response"] is False
    assert out["response"] == "" and out["conversation_id"] == CHAT_A


# ── streaming and timeouts ───────────────────────────────────────────────


def test_deltas_reach_the_sink_and_the_unstreamed_rest_follows():
    adapter, page, _ = make()
    got: list[str] = []

    def script(on_delta=None, **kw):
        on_delta("hel")
        on_delta("lo")
        return {"response": "hello world", "url": FOLDER_URL, "conversation_id": None}

    adapter._fast.script = script
    out = adapter.execute("send", {"message": "x"}, on_delta=got.append)
    assert got == ["hel", "lo", " world"]
    assert "".join(got) == out["response"]


def test_a_whole_rest_reply_is_delivered_as_one_delta():
    adapter, page, _ = make()
    got: list[str] = []
    adapter._fast.script = lambda **kw: {"response": "whole", "url": FOLDER_URL, "conversation_id": None}
    adapter.execute("send", {"message": "x"}, on_delta=got.append)
    assert got == ["whole"]


def test_a_reflowed_reply_is_not_duplicated_in_the_stream():
    adapter, page, _ = make()
    got: list[str] = []

    def script(on_delta=None, **kw):
        on_delta("**he")
        return {"response": "hello", "url": FOLDER_URL, "conversation_id": None}

    adapter._fast.script = script
    out = adapter.execute("send", {"message": "x"}, on_delta=got.append)
    assert got == ["**he"] and out["response"] == "hello"


def test_failing_sink_does_not_break_the_send():
    adapter, page, _ = make()
    adapter._fast.script = lambda **kw: {"response": "text", "url": FOLDER_URL, "conversation_id": None}

    def sink(_):
        raise BrokenPipeError

    assert adapter.execute("send", {"message": "x"}, on_delta=sink)["response"] == "text"


def test_timeout_carries_the_partial_reply_and_the_conversation():
    adapter, page, _ = make()
    got: list[str] = []

    def script(on_delta=None, **kw):
        on_delta("partial ")
        page.url = chat_url(CHAT_A)
        return {"response": "partial answer", "url": page.url, "conversation_id": CHAT_A,
                "timed_out": True}

    adapter._fast.script = script
    with pytest.raises(GrokServiceError) as exc:
        adapter.execute("send", {"message": "x", "timeout_s": 5}, on_delta=got.append)
    err = exc.value
    assert (err.code, err.status) == ("timeout", 504)
    assert err.detail == "partial answer" and err.data["partial"] == "partial answer"
    assert err.data["result"]["conversation_id"] == CHAT_A
    assert err.data["result"]["url"] == chat_url(CHAT_A)
    assert "".join(got) == "partial answer"
    assert adapter._members[CHAT_A]  # the conversation is not lost
    assert adapter._client.names() == []  # and a timeout is not a reason to re-send


def test_timeout_with_no_text_still_reports_a_timeout():
    adapter, page, _ = make()
    adapter._fast.script = lambda **kw: {"response": "", "url": FOLDER_URL, "conversation_id": None,
                                         "timed_out": True}
    with pytest.raises(GrokServiceError) as exc:
        adapter.execute("send", {"message": "x"})
    assert exc.value.code == "timeout" and exc.value.data["partial"] == ""


def test_regenerate_timeout_maps_to_a_timeout_error():
    adapter, page, _ = make()
    page.dom["list"] = [item(CHAT_A)]
    adapter._client.regenerate_result = {"ok": True, "response": "half", "reason": "timeout"}
    with pytest.raises(GrokServiceError) as exc:
        adapter.execute("regenerate", {"conversation_url": chat_url(CHAT_A), "timeout_s": 3})
    assert exc.value.code == "timeout" and exc.value.data["partial"] == "half"
    assert ("regenerate", chat_url(CHAT_A), 3) in adapter._client.calls


# ── stop ─────────────────────────────────────────────────────────────────


def test_stop_page_is_static_and_needs_no_adapter_or_lock():
    page = FakePage(url=chat_url(CHAT_A), stop=True)
    adapter = GrokSessionAdapter(FakeRuntime(), FakePage(), folder="NSFW", folder_url=FOLDER_URL)
    holder_ready, release = threading.Event(), threading.Event()

    def hold():
        with adapter._client._lock:
            holder_ready.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    try:
        assert holder_ready.wait(5)
        assert GrokSessionAdapter.stop_page(page) is True  # not queued behind the held lock
    finally:
        release.set()
        t.join(5)
    page.dom["stop"] = False
    assert GrokSessionAdapter.stop_page(page) is False
    assert page.gotos == []


def test_stop_on_the_current_project_page_does_not_navigate():
    adapter, page, _ = make(url=chat_url(CHAT_A), stop=True)
    adapter._members[CHAT_A] = chat_url(CHAT_A)
    out = adapter.execute("stop", {})
    assert out is True and page.gotos == []


def test_stop_refuses_a_page_outside_the_folder():
    adapter, page, _ = make(url=f"https://grok.com/c/{FOREIGN}", stop=True)
    with pytest.raises(ServiceError) as exc:
        adapter.execute("stop", {})
    assert exc.value.code == "folder_mismatch"


def test_stop_for_a_named_member_does_not_navigate_when_already_there():
    adapter, page, _ = make(url=chat_url(CHAT_A), stop=True)
    adapter._members[CHAT_A] = chat_url(CHAT_A)
    assert adapter.execute("stop", {"conversation_url": chat_url(CHAT_A)}) is True
    assert page.gotos == []


# ── other operations ─────────────────────────────────────────────────────


def test_models_are_read_on_the_project_surface():
    adapter, page, _ = make(url=chat_url(CHAT_A))
    assert adapter.execute("get_models", {}) == ["grok-4", "grok-3"]
    assert adapter.execute("switch_model", {"model_name": "grok-3"}) is True
    assert page.gotos[0] == FOLDER_URL


def test_read_refuses_a_page_that_ended_on_another_conversation():
    adapter, page, _ = make()
    page.dom["list"] = [item(CHAT_A)]
    adapter._client.read_result = {"url": chat_url(CHAT_B), "conversation_id": CHAT_B, "messages": []}
    with pytest.raises(ServiceError) as exc:
        adapter.execute("read", {"conversation_url": chat_url(CHAT_A)})
    assert exc.value.code == "page_deviation"


def test_url_is_the_bound_conversation_or_none():
    """Core stores ``adapter.url`` as the session's conversation (``None``: unchanged)."""
    adapter, page, _ = make(url=FOLDER_URL)
    assert adapter.url is None  # the landing is not a conversation
    page.url = chat_url(CHAT_A) + "&rid=1"
    assert adapter.url == chat_url(CHAT_A) + "&rid=1"  # the actual project URL
    page.url = f"https://grok.com/c/{CHAT_A}"
    assert adapter.url == chat_url(CHAT_A)  # canonical project form, re-verifiable
    page.url = "about:blank"
    assert adapter.url is None
    page.url = f"https://example.com/c/{CHAT_A}"
    assert adapter.url is None


@pytest.mark.parametrize("marker", [FOLDER_URL, FOLDER_URL + "/", "https://grok.com/", "https://grok.com"])
def test_a_stored_new_chat_url_starts_a_project_conversation(marker):
    adapter, page, _ = make()
    adapter.execute("send", {"message": "x", "conversation_url": marker})
    assert page.gotos == [FOLDER_URL]  # never the home, never a membership lookup
    assert adapter._fast.calls[0]["conversation_url"] == FOLDER_URL


def test_results_have_the_shapes_the_service_clients_expect():
    adapter, page, _ = make()
    page.dom["list"] = [item(CHAT_A)]
    assert isinstance(adapter.execute("list_conversations", {}), list)
    assert adapter.execute("get_models", {}) == ["grok-4", "grok-3"]
    for op, kwargs in (("delete", {"conversation_url": chat_url(CHAT_A)}),
                       ("archive", {"conversation_url": chat_url(CHAT_A)}),
                       ("rename", {"conversation_url": chat_url(CHAT_A), "new_title": "t"}),
                       ("switch_model", {"model_name": "grok-4"})):
        assert adapter.execute(op, kwargs) is True
    cleanup = adapter.execute("cleanup_threads", {"keep_recent": 0})
    assert isinstance(cleanup["summary"], str) and isinstance(cleanup["deleted_ids"], list)
    for op in ("send", "new_chat"):
        assert isinstance(adapter.execute(op, {"message": "x"} if op == "send" else {}), dict)


def test_send_does_not_claim_an_unscoped_conversation_is_in_the_project():
    adapter, page, _ = make()

    def submitted(**kwargs):
        page.url = f"https://grok.com/c/{CHAT_A}"
        return {"response": "reply", "url": page.url, "conversation_id": CHAT_A}

    adapter._fast.script = submitted
    with pytest.raises(ServiceError) as exc:
        adapter.execute("send", {"message": "once"})
    assert exc.value.code == "folder_mismatch"
    assert len(adapter._fast.calls) == 1 and adapter._client.calls == []
    assert CHAT_A not in adapter._members


def test_moving_a_cached_conversation_out_of_the_project_blocks_continuation():
    adapter, page, _ = make(url=chat_url(CHAT_A))
    adapter._members[CHAT_A] = chat_url(CHAT_A)
    with pytest.raises(ServiceError) as exc:
        adapter.execute("send", {"message": "never send", "conversation_url": chat_url(CHAT_A)})
    assert exc.value.code == "folder_mismatch"
    assert adapter._fast.calls == [] and adapter._client.calls == []
