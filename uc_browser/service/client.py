"""Plain-HTTP client for the UC service (wire contract v0).

Pure standard library: importing this module must never import Playwright or
any browser module, so a consumer with no browser library can fetch, read
cookies, snapshot, run site operations and control service-made pages. The
resolution of a target id to a local ``Page`` lives in
``uc_browser.service.targets`` and is loaded only by ``UCBrowser``.

Errors:

* ``UCServiceGone`` -- the service could not be reached, or dropped the
  connection (defined in ``uc_browser.service.errors``, re-exported here).
* ``ServiceError`` -- the service answered with ``{"error", "detail"}``.
* ``ControlsUnavailable`` -- a ``ServiceError`` (409 ``no_controls``) that
  ``control`` raises for a window configured with ``engine: none``. It carries
  no value: only ``UCBrowser`` can run the method's own body locally.

A read timeout is not a dead service and propagates as ``TimeoutError``.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping, Optional

from uc_browser.service.errors import ServiceError, UCServiceGone

__all__ = [
    "ControlsUnavailable",
    "ServiceClient",
    "ServiceError",
    "ServiceStream",
    "UCServiceGone",
]


class ControlsUnavailable(ServiceError):
    """409 ``no_controls``: the window has no engine, so the service refused
    the control before touching the page."""

    def __init__(self, window: str, name: str, detail: Optional[str] = None):
        super().__init__(
            "no_controls",
            detail or f"window {window} has no controls (engine none); cannot run {name}",
            409,
        )
        self.window = window
        self.name = name


def _seg(value: Any) -> str:
    """One URL path segment: everything, including ``/``, is escaped."""
    return urllib.parse.quote(str(value), safe="")


def _path(*parts: Any) -> str:
    return "/" + "/".join(_seg(p) for p in parts)


def _wire_error(status: int, raw: bytes) -> ServiceError:
    text = raw.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return ServiceError(payload["error"], str(payload.get("detail", "")), status)
    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        return ServiceError(
            f"http_{status}", detail if isinstance(detail, str) else json.dumps(detail), status,
        )
    return ServiceError(f"http_{status}", text[:500], status)


class ServiceStream:
    """Iterator over a newline-delimited JSON response.

    Closing it (``close()``, leaving a ``with`` block, garbage collection, or
    closing a generator that iterates it) closes the HTTP response, which is
    how the service learns the caller went away. A stream that ends without
    its terminal line (``{"status"}`` / ``{"result"}``) was cut short:
    callers must check for it.
    """

    def __init__(self, response, url: str, *, raise_errors: bool = False):
        self._resp = response
        self._url = url
        self._raise_errors = raise_errors

    @property
    def closed(self) -> bool:
        return self._resp is None

    def close(self) -> None:
        resp, self._resp = self._resp, None
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001 - nothing useful to do
                pass

    def __enter__(self) -> "ServiceStream":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def __iter__(self) -> "ServiceStream":
        return self

    def __next__(self) -> Any:
        while True:
            resp = self._resp
            if resp is None:
                raise StopIteration
            try:
                line = resp.readline()
            except (ConnectionError, http.client.HTTPException) as exc:
                self.close()
                raise UCServiceGone(f"UC service {self._url} dropped the stream: {exc}") from exc
            except BaseException:
                self.close()
                raise
            if not line:
                self.close()
                raise StopIteration
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                self.close()
                raise ServiceError("bad_response", f"non-JSON stream line from {self._url}", 502) from exc
            if self._raise_errors and isinstance(obj, dict) and obj.get("error") is not None:
                self.close()
                err = obj["error"]
                if isinstance(err, dict):
                    raise ServiceError(
                        str(err.get("error") or err.get("code") or "error"),
                        str(err.get("detail", "")), 500,
                    )
                raise ServiceError(str(err), str(obj.get("detail", "")), 500)
            return obj


class ServiceClient:
    """Stateless HTTP helper. Safe to share between threads (one connection
    per request)."""

    def __init__(
        self,
        url: Optional[str] = None,
        *,
        timeout: Optional[float] = 30.0,
        control_timeout: Optional[float] = None,
    ):
        url = url if url is not None else os.environ.get("UC_SERVICE_URL", "")
        url = url.strip().rstrip("/")
        if not url:
            raise ValueError("ServiceClient needs a URL or UC_SERVICE_URL")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"UC service URL must be http(s): {url!r}")
        self.url = url
        self.timeout = timeout
        self.control_timeout = control_timeout
        # Loopback service: never route through an environment proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ── generic ─────────────────────────────────────────────────────

    def _open(self, method: str, path: str, body: Any, query: Optional[Mapping[str, Any]], timeout):
        full = self.url + path
        if query:
            full += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(full, data=data, method=method.upper(), headers=headers)
        try:
            return self._opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            finally:
                exc.close()
            raise _wire_error(exc.code, raw) from None
        except urllib.error.URLError as exc:
            raise UCServiceGone(f"UC service {self.url} unreachable: {exc.reason}") from exc
        except (ConnectionError, http.client.HTTPException) as exc:
            raise UCServiceGone(f"UC service {self.url} dropped the connection: {exc}") from exc

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: Optional[Mapping[str, Any]] = None,
        timeout: Any = ...,
    ) -> Any:
        """One JSON request. ``path`` is used verbatim (already escaped; the
        route helpers escape every identifier). Returns the parsed JSON
        response, ``None`` for an empty body."""
        resp = self._open(method, path, body, query, self.timeout if timeout is ... else timeout)
        try:
            raw = resp.read()
        except (ConnectionError, http.client.HTTPException) as exc:
            raise UCServiceGone(f"UC service {self.url} dropped the connection: {exc}") from exc
        finally:
            resp.close()
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ServiceError("bad_response", f"non-JSON response from {path}", 502) from exc

    def stream(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: Optional[Mapping[str, Any]] = None,
        timeout: Any = None,
        raise_errors: bool = False,
    ) -> ServiceStream:
        """Open a newline-delimited JSON response. With ``raise_errors`` an
        ``{"error": ...}`` line raises ``ServiceError``."""
        resp = self._open(method, path, body, query, timeout)
        return ServiceStream(resp, self.url + path, raise_errors=raise_errors)

    # ── windows, clients, pages ─────────────────────────────────────

    def windows(self) -> dict:
        return self.request("GET", "/windows")

    def start_window(self, window: str) -> dict:
        return self.request("POST", _path("windows", window, "start"), timeout=None)

    def register_client(self, window: str, label: Optional[str] = None, pid: Optional[int] = None) -> str:
        body: dict = {"pid": os.getpid() if pid is None else pid, "window": window}
        if label:
            body["label"] = label
        return self.request("POST", "/clients", body)["client_id"]

    def close_client(self, client_id: str) -> dict:
        return self.request("DELETE", _path("clients", client_id))

    def revoke(self, label: str) -> dict:
        return self.request("DELETE", "/clients", query={"label": label})

    def new_page(
        self,
        window: str,
        client_id: Optional[str] = None,
        url: Optional[str] = None,
        wait_ms: Optional[int] = None,
    ) -> str:
        body: dict = {}
        if client_id is not None:
            body["client_id"] = client_id
        if url is not None:
            body["url"] = url
        if wait_ms is not None:
            body["wait_ms"] = wait_ms
        return self.request("POST", _path("windows", window, "pages"), body)["target_id"]

    def close_page(self, window: str, target_id: str) -> dict:
        return self.request("DELETE", _path("windows", window, "pages", target_id))

    # ── cookies, fetch, snapshot ────────────────────────────────────

    def add_cookies(
        self,
        window: str,
        cookies: Optional[Iterable[Mapping[str, Any]]] = None,
        from_chrome: Optional[Iterable[str]] = None,
    ) -> dict:
        body: dict = {}
        if cookies is not None:
            body["cookies"] = list(cookies)
        if from_chrome is not None:
            body["from_chrome"] = list(from_chrome)
        return self.request("POST", _path("windows", window, "cookies"), body)

    def get_cookies(self, window: str, domain: Optional[str] = None) -> list:
        query = {"domain": domain} if domain is not None else None
        return self.request("GET", _path("windows", window, "cookies"), query=query)["cookies"]

    def fetch(
        self,
        window: str,
        url: str,
        *,
        method: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        body: Any = None,
        from_url: Optional[str] = None,
        stream: bool = False,
    ):
        """In-page fetch. Unstreamed: ``{status, headers, body}``. Streamed: a
        ``ServiceStream`` of ``{"chunk"}`` lines then ``{"status"}``."""
        payload: dict = {"url": url}
        if method is not None:
            payload["method"] = method
        if headers is not None:
            payload["headers"] = dict(headers)
        if body is not None:
            payload["body"] = body
        if from_url is not None:
            payload["from"] = from_url
        path = _path("windows", window, "fetch")
        if stream:
            payload["stream"] = True
            return self.stream("POST", path, payload)
        return self.request("POST", path, payload, timeout=None)

    def snapshot(self, window: str, target_id: str, format: str = "png", full_page: Optional[bool] = None):
        """``bytes`` for ``png`` (decoded from base64), ``str`` otherwise."""
        body: dict = {"format": format}
        if full_page is not None:
            body["full_page"] = bool(full_page)
        data = self.request("POST", _path("windows", window, "pages", target_id, "snapshot"), body, timeout=None)["data"]
        return base64.b64decode(data) if format == "png" else data

    # ── controls ────────────────────────────────────────────────────

    def control(
        self,
        window: str,
        target_id: str,
        name: str,
        args: Iterable[Any] = (),
        kwargs: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Any = ...,
    ) -> Any:
        """Run a ``UCBrowser`` page control on the service; returns ``result``.
        Raises ``ControlsUnavailable`` on 409 ``no_controls``."""
        body = {"args": list(args), "kwargs": dict(kwargs or {})}
        try:
            reply = self.request(
                "POST",
                _path("windows", window, "pages", target_id, "controls", name),
                body,
                timeout=self.control_timeout if timeout is ... else timeout,
            )
        except ServiceError as exc:
            if exc.status == 409 and exc.code == "no_controls":
                raise ControlsUnavailable(window, name, exc.detail) from None
            raise
        return reply["result"]

    # ── sites ───────────────────────────────────────────────────────

    def site(
        self,
        site: str,
        op: str,
        kwargs: Optional[Mapping[str, Any]] = None,
        *,
        session_id: Optional[str] = None,
        stream: bool = False,
        wait: Optional[bool] = None,
        timeout: Any = None,
    ):
        """``POST /sites/{site}/{op}``. Unstreamed: the ``result``. Streamed: a
        ``ServiceStream`` of ``{"delta"}`` lines then ``{"result"}``; an
        ``{"error"}`` line raises ``ServiceError``."""
        body = dict(kwargs or {})
        if session_id is not None:
            body["session_id"] = session_id
        if wait is not None:
            body["wait"] = wait
        path = _path("sites", site, op)
        if stream:
            body["stream"] = True
            return self.stream("POST", path, body, timeout=timeout, raise_errors=True)
        return self.request("POST", path, body, timeout=timeout)["result"]

    def site_sessions(self, site: str) -> dict:
        return self.request("GET", _path("sites", site, "sessions"))

    def forget_session(self, site: str, session_id: str) -> dict:
        return self.request("DELETE", _path("sites", site, "sessions", session_id))
