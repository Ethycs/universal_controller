"""Wire contract v0. Handlers exchange JSON and futures, never browser objects."""
from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .errors import ServiceError


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class RegisterBody(Body):
    pid: StrictInt
    label: str = ""
    window: str = "default"
    unattached: bool = False


class PageBody(Body):
    client_id: str
    url: str | None = None
    wait_ms: int = Field(default=0, ge=0)
    lifetime: Literal["client", "window"] = "client"


class PageLifetimeBody(Body):
    client_id: str
    lifetime: Literal["client", "window"]


class ControlBody(Body):
    args: list[Any] = Field(default_factory=list)
    kwargs: dict[str, Any] = Field(default_factory=dict)


class CookiesBody(Body):
    cookies: list[dict[str, Any]] | None = None
    from_chrome: list[str] | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if (self.cookies is None) == (self.from_chrome is None):
            raise ValueError("provide cookies or from_chrome, not both")
        return self


class FetchBody(Body):
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    from_url: str | None = Field(default=None, alias="from")
    stream: bool = False


class SnapshotBody(Body):
    format: str
    full_page: bool = False


def create_router(manager, leases, operations) -> APIRouter:
    router = APIRouter()

    @router.get("/windows")
    async def windows():
        def listing():
            return {name: leases.window_info(name) for name in manager.windows}
        return await asyncio.to_thread(listing)

    @router.post("/windows/{name}/start")
    async def start(name: str):
        window = manager.get(name)
        await asyncio.to_thread(window.start)
        return await asyncio.to_thread(leases.window_info, name)

    @router.post("/clients")
    async def register(body: RegisterBody):
        if body.unattached:
            client_id = await asyncio.to_thread(leases.register, body.pid, body.label, body.window, unattached=True)
        else:
            client_id = await asyncio.to_thread(leases.register, body.pid, body.label, body.window)
        return {"client_id": client_id}

    @router.delete("/clients")
    async def revoke(label: str = Query(...)):
        return await asyncio.to_thread(leases.revoke, label)

    @router.delete("/clients/{client_id}")
    async def release(client_id: str):
        return {"closed": await asyncio.to_thread(leases.release, client_id)}

    @router.post("/windows/{name}/pages")
    async def new_page(name: str, body: PageBody):
        # Default lifetime keeps the original create_page call shape.
        lifetime = {} if body.lifetime == "client" else {"lifetime": body.lifetime}
        if await asyncio.to_thread(leases.is_unattached_client, name, body.client_id):
            # A quiet client's page is made over DevTools HTTP: no worker
            # attaches, so there is nothing to navigate or wait on.
            if body.wait_ms:
                raise ServiceError("wait_unsupported", "wait_ms is not supported for an unattached client", 400)
            target = await asyncio.to_thread(leases.create_page, name, body.client_id, url=body.url, **lifetime)
            return {"target_id": target}
        target = await asyncio.to_thread(leases.create_page, name, body.client_id, **lifetime)
        if body.url is not None:
            try:
                await asyncio.to_thread(operations.navigate, name, target, body.url, body.wait_ms)
            except Exception:
                try:
                    await asyncio.to_thread(leases.close_page, name, target)
                except ServiceError as exc:
                    if exc.code != "no_such_target":
                        raise
                raise
        return {"target_id": target}

    @router.put("/windows/{name}/pages/{target}/lifetime")
    async def set_page_lifetime(name: str, target: str, body: PageLifetimeBody):
        return await asyncio.to_thread(leases.set_page_lifetime, name, target, body.client_id, body.lifetime)

    @router.get("/windows/{name}/pages/{target}")
    async def page_info(name: str, target: str):
        window = manager.get(name)
        pages = await asyncio.to_thread(lambda: window.owner(lambda rt: rt.pages_info(), launch=False).result())
        for page in pages:
            if page["target_id"] == target:
                return page
        raise ServiceError("no_such_target", "no such page", 404)

    @router.delete("/windows/{name}/pages/{target}")
    async def close_page(name: str, target: str):
        return {"closed": await asyncio.to_thread(leases.close_page, name, target)}

    @router.post("/windows/{name}/pages/{target}/controls/{control}")
    async def control(name: str, target: str, control: str, body: ControlBody):
        from uc_browser.browser import UCBrowser
        method = getattr(UCBrowser, control, None)
        if control.startswith("_") or not inspect.isfunction(method) or list(inspect.signature(method).parameters)[1:2] != ["page"]:
            raise ServiceError("no_such_control", "unknown page control", 404)
        try:
            inspect.signature(method).bind(None, None, *body.args, **body.kwargs)
            json.dumps([body.args, body.kwargs], allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ServiceError("bad_arguments", "control arguments do not match its JSON signature", 400) from exc
        window = manager.get(name)
        future = await asyncio.to_thread(window.control, target, control, body.args, body.kwargs)
        result = await asyncio.wrap_future(future)
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ServiceError("control_failed", "control returned a non-JSON result", 500) from exc
        return {"result": result}

    @router.post("/windows/{name}/cookies")
    async def add_cookies(name: str, body: CookiesBody):
        return await asyncio.to_thread(operations.add_cookies, name, cookies=body.cookies, from_chrome=body.from_chrome)

    @router.get("/windows/{name}/cookies")
    async def cookies(name: str, domain: str | None = None):
        return await asyncio.to_thread(operations.cookies, name, domain)

    @router.post("/windows/{name}/pages/{target}/snapshot")
    async def snapshot(name: str, target: str, body: SnapshotBody):
        return await asyncio.to_thread(operations.snapshot, name, target, body.format, body.full_page)

    @router.post("/windows/{name}/fetch")
    async def fetch(name: str, body: FetchBody):
        request = body.model_dump(by_alias=True, exclude_none=True)
        if not body.stream:
            return await asyncio.to_thread(operations.fetch, name, request)
        events, cancel, future = await asyncio.to_thread(operations.stream, name, request)
        try:
            first = await asyncio.to_thread(events.get)
            if isinstance(first, ServiceError):
                raise first
            if first is None or "ready" not in first:
                raise ServiceError("origin_unreachable", "stream ended before a response", 502)
        except BaseException:
            cancel()
            raise

        async def chunks():
            try:
                while True:
                    event = await asyncio.to_thread(events.get)
                    if event is None:
                        break
                    if isinstance(event, ServiceError):
                        yield json.dumps(event.as_dict()) + "\n"
                    else:
                        yield json.dumps(event) + "\n"
            finally:
                cancel()
        return StreamingResponse(chunks(), media_type="application/x-ndjson")

    return router
