#!/usr/bin/env python3
"""Verify the pinned FastAPI stack through sync TestClient and async ASGI."""

from __future__ import annotations

import asyncio
import importlib.metadata
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx


MAX_REQUEST_SECONDS = 5.0
EXPECTED = {
    "fastapi": "0.115.6",
    "starlette": "0.41.3",
    "httpx": "0.28.1",
    "anyio": "4.14.0",
}


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/smoke")
    async def smoke() -> dict[str, bool]:
        return {"ok": True}

    return app


def _assert_elapsed(label: str, started: float) -> float:
    elapsed = time.perf_counter() - started
    if elapsed >= MAX_REQUEST_SECONDS:
        raise AssertionError(
            f"{label} took {elapsed:.3f}s; limit is {MAX_REQUEST_SECONDS:.1f}s"
        )
    return elapsed


def _sync_smoke(app: FastAPI) -> float:
    started = time.perf_counter()
    with TestClient(app) as client:
        response = client.get("/smoke")
    response.raise_for_status()
    assert response.json() == {"ok": True}
    return _assert_elapsed("TestClient request", started)


async def _async_smoke(app: FastAPI) -> float:
    started = time.perf_counter()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/smoke")
    response.raise_for_status()
    assert response.json() == {"ok": True}
    return _assert_elapsed("ASGITransport request", started)


def main() -> int:
    versions = {name: importlib.metadata.version(name) for name in EXPECTED}
    if versions != EXPECTED:
        raise SystemExit(f"unexpected ASGI dependency set: {versions}; expected {EXPECTED}")
    app = _app()
    sync_elapsed = _sync_smoke(app)
    async_elapsed = asyncio.run(_async_smoke(app))
    print(
        "ASGI smoke passed "
        f"(TestClient={sync_elapsed:.3f}s, ASGITransport={async_elapsed:.3f}s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
