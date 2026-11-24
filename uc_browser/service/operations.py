"""HTTP-only operations executed on window-owned browser threads."""
from __future__ import annotations

import base64
import queue
import threading
import uuid
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import ServiceError

FETCH_JS = """async ({url, method, headers, body, referrer, callback, token}) => {
  const abort = new AbortController();
  window.__ucServiceFetches ??= {};
  window.__ucServiceFetches[token] = abort;
  try {
    const init = {method, headers, credentials: 'include', signal: abort.signal};
    if (referrer) {
      // Chromium can substitute the document referrer across a redirect.
      // Keep an explicit caller origin from silently changing identity.
      if (new URL(referrer).origin !== location.origin)
        throw new Error('fetch referrer origin differs from the retained page');
      init.referrer = referrer;
    }
    if (body !== null && body !== undefined)
      init.body = typeof body === 'string' ? body : JSON.stringify(body);
    const response = await window.fetch(url, init);
    if (!callback) return {status: response.status,
      headers: Object.fromEntries(response.headers), body: await response.text()};
    await window[callback]({ready: response.status});
    if (response.body) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, {stream: true});
        if (chunk) await window[callback]({chunk});
      }
      const last = decoder.decode();
      if (last) await window[callback]({chunk: last});
    }
    return {status: response.status};
  } finally {
    delete window.__ucServiceFetches[token];
  }
}"""


def origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ServiceError("origin_unreachable", "fetch requires an HTTP(S) URL without credentials", 502)
    try:
        parsed.port
    except ValueError as exc:
        raise ServiceError("origin_unreachable", "invalid URL port", 502) from exc
    return f"{parsed.scheme}://{parsed.netloc}"


def _page_origin(url: str) -> str | None:
    """Origin of a live page URL, or None for about:blank and other non-HTTP pages."""
    try:
        return origin(url)
    except ServiceError:
        return None


@dataclass
class FetchPage:
    generation: int
    target: str
    url: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def _unattached(window) -> bool:
    return bool(getattr(window.spec, "unattached", False))


def _runtime_job(window, fn):
    """Run a Playwright-touching ``fn`` where the window allows it. An
    unattached window's owner holds no Playwright, so it takes a temporary
    worker attachment; other windows keep the owner."""
    return window.work(fn) if _unattached(window) else window.owner(fn)


class Operations:
    def __init__(self, manager, leases):
        self.manager = manager
        self.leases = leases
        self._pages: dict[tuple[str, str], FetchPage] = {}
        self._lock = threading.Lock()

    def navigate(self, name: str, target: str, url: str, wait_ms: int = 0):
        window = self.manager.get(name)
        def job(rt):
            page = rt.resolve(target)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                if wait_ms:
                    page.wait_for_timeout(wait_ms)
            except Exception as exc:
                raise ServiceError("navigation_failed", "page navigation failed", 502) from exc
        return window.work(job).result()

    def add_cookies(self, name: str, *, cookies=None, from_chrome=None) -> dict:
        window = self.manager.get(name)
        if from_chrome is not None:
            try:
                import rookiepy
            except ImportError as exc:
                raise ServiceError("chrome_cookies_unavailable", "install the chrome-cookies extra", 501) from exc
            from uc_browser.chrome_cookies import cookies_to_playwright
            try:
                cookies = cookies_to_playwright(rookiepy.chrome(from_chrome))
            except Exception as exc:
                # Never include cookie material or upstream exception text.
                raise ServiceError("chrome_cookies_unavailable", "Chrome cookie extraction failed", 501) from exc
        cookies = cookies or []
        _runtime_job(window, lambda rt: rt.context.add_cookies(cookies)).result()
        return {"added": len(cookies)}

    def cookies(self, name: str, domain: str | None) -> dict:
        window = self.manager.get(name)
        if not domain or "/" in domain or ":" in domain:
            raise ServiceError("domain_required", "provide a cookie domain", 400)
        domain = domain.lower().lstrip(".")
        def job(rt):
            # A cookie is applicable when the requested host is its domain
            # or a subdomain of a domain cookie. Never return sibling jars.
            return [c for c in rt.context.cookies()
                    if c["domain"].lower().lstrip(".") == domain
                    or (c["domain"].startswith(".")
                        and domain.endswith(c["domain"].lower()))]
        return {"cookies": _runtime_job(window, job).result()}

    def snapshot(self, name: str, target: str, format: str, full_page: bool = False) -> dict:
        window = self.manager.get(name)
        if format not in ("png", "html", "mhtml") or (format == "mhtml" and window.spec.browser not in ("chromium", "chrome")):
            raise ServiceError("format_unsupported", "snapshot format unavailable on this window", 409)
        def job(rt):
            page = rt.resolve(target)
            if format == "html":
                return page.content()
            if format == "png":
                return base64.b64encode(page.screenshot(full_page=full_page)).decode("ascii")
            session = rt.context.new_cdp_session(page)
            try:
                return session.send("Page.captureSnapshot", {"format": "mhtml"})["data"]
            finally:
                session.detach()
        return {"data": window.work(job).result()}

    def _fetch_page(self, name: str, from_url: str) -> FetchPage:
        window = self.manager.get(name)
        key = name, origin(from_url)
        with self._lock:
            window.start()
            entry = self._pages.get(key)
            # A client may close any target over CDP, including our fetch tab.
            live = {p["target_id"] for p in window.info()["pages"]}
            if entry is None or entry.generation != window.generation or entry.target not in live:
                entry = FetchPage(window.generation, self.leases.internal_page(name))
                self._pages[key] = entry
            return entry

    def _fetch_job(self, name, request, *, emit=None, cancelled=None):
        url = request["url"]
        origin(url)
        explicit_from = request.get("from")
        # One page per (window, origin of the referring URL). Without an
        # explicit "from" the fetch URL's own origin root is used.
        from_url = explicit_from or origin(url) + "/"
        entry = self._fetch_page(name, from_url)
        window = self.manager.get(name)
        token = uuid.uuid4().hex
        # Stream and buffered fetches share every request option; only the
        # streaming callback differs.
        options = {
            "url": url, "method": request.get("method", "GET"),
            "headers": request.get("headers", {}), "body": request.get("body"),
            "token": token,
        }

        def job(rt):
            with entry.lock:
                if cancelled and cancelled.is_set():
                    return None
                page = rt.resolve(entry.target)
                callback = None
                holder = None
                try:
                    # Navigate only to establish the origin (first use, a fresh
                    # page after a generation change or closed tab, or a page
                    # something else moved off the origin). Later same-origin
                    # "from" values are sent as the Fetch referrer instead, so
                    # navigation never aborts or races a fetch on this page.
                    landed = _page_origin(entry.url) if entry.url else None
                    if landed is None or _page_origin(page.url) != landed:
                        page.goto(from_url, wait_until="domcontentloaded", timeout=30000)
                        entry.url = page.url
                    # Preserve the requested referrer. If navigation redirected
                    # across origins, Fetch must reject it rather than silently
                    # substituting the redirected document's identity.
                    referrer = explicit_from
                    if emit:
                        bindings = getattr(rt, "_fetch_bindings", None)
                        if bindings is None:
                            bindings = rt._fetch_bindings = {}
                        for old in list(bindings):
                            if old.is_closed():
                                del bindings[old]
                        binding = bindings.get(page)
                        if binding is None:
                            callback = "__ucFetch_" + uuid.uuid4().hex
                            holder = {"emit": None}
                            page.expose_function(callback, lambda event: holder["emit"](event))
                            bindings[page] = callback, holder
                        else:
                            callback, holder = binding
                        holder["emit"] = emit
                    return page.evaluate(FETCH_JS, {**options, "referrer": referrer, "callback": callback})
                except ServiceError:
                    raise
                except Exception as exc:
                    raise ServiceError("origin_unreachable", "in-page fetch or origin navigation failed", 502) from exc
                finally:
                    if holder is not None:
                        holder["emit"] = None
        return window, entry, token, window.work(job, long=True)

    def fetch(self, name: str, request: dict) -> dict:
        return self._fetch_job(name, request)[3].result()

    def stream(self, name: str, request: dict):
        """Return a bounded event queue, cancellation callback, and completion future."""
        events = queue.Queue(maxsize=64)
        cancelled = threading.Event()
        def emit(event):
            while not cancelled.is_set():
                try:
                    events.put(event, timeout=0.1)
                    return
                except queue.Full:
                    continue
        window, entry, token, future = self._fetch_job(name, request, emit=emit, cancelled=cancelled)
        def finished(fut):
            try:
                result = fut.result()
                if result is not None:
                    emit(result)
            except ServiceError as exc:
                emit(exc)
            except Exception:
                emit(ServiceError("origin_unreachable", "stream failed", 502))
            finally:
                emit(None)
        future.add_done_callback(finished)
        def cancel():
            cancelled.set()
            future.cancel()
            if not future.done() and window.running:
                # Another attachment can interrupt a pending fetch without
                # touching the worker's Playwright objects across threads.
                _runtime_job(window, lambda rt: rt.resolve(entry.target).evaluate(
                    "token => window.__ucServiceFetches?.[token]?.abort()", token))
        return events, cancel, future
