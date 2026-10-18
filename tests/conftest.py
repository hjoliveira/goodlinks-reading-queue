"""Shared harness for the test suite.

Every HTTP call the code under test makes is served by an httpx MockTransport,
so the tests need no GoodLinks instance, no network, and no .env.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any
from unittest import mock

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import goodlinks_client as gl
import goodlinks_mcp as mcp_srv
import goodlinks_server as viewer


@contextmanager
def mock_api(handler: Callable[[httpx.Request], httpx.Response]):
    """Serve every AsyncClient request from `handler` instead of the network.

    The client builds its own httpx.AsyncClient per call, so the seam is the
    constructor: patching it lets the production code stay unaware of tests.
    """
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    with mock.patch.object(httpx, "AsyncClient", factory):
        yield


def json_route(routes: dict[str, Any], record: list[httpx.Request] | None = None):
    """Handler that maps URL path -> payload, 404ing anything unrouted."""

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        for path, payload in routes.items():
            if request.url.path.endswith(path):
                if isinstance(payload, httpx.Response):
                    return payload
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "no route"})

    return handler


def links_page(count: int, has_more: bool = False, start: int = 0) -> dict[str, Any]:
    """A /lists/{list} response body holding `count` synthetic links."""
    return {
        "data": [
            {
                "id": f"id{i:03d}",
                "url": f"https://example.com/{i}",
                "title": f"Article {i}",
                "summary": f"Summary {i}",
                "author": "Ada" if i % 2 else None,
                "tags": ["tech/py"] if i % 2 else [],
                "wordCount": 230 * (i + 1),
                "starred": i % 2 == 0,
                "highlighted": False,
                "addedAt": "2026-08-01T10:00:00Z",
                "modifiedAt": "2026-08-02T10:00:00Z",
                "readAt": "2026-08-03T10:00:00Z" if i % 2 else None,
            }
            for i in range(start, start + count)
        ],
        "hasMore": has_more,
    }


def call_tool(name: str, args: dict[str, Any]):
    """Call an MCP tool, normalising the raise-vs-return error paths.

    In-process call_tool raises ToolError; over the wire the same failure comes
    back as isError. Tests care about the message either way.
    """

    async def run():
        try:
            result = await mcp_srv.mcp.call_tool(name, args)
        except ToolError as exc:
            return True, str(exc), None
        except Exception as exc:  # schema validation, etc.
            return True, f"{type(exc).__name__}: {exc}", None
        text = result.content[0].text if result.content else ""
        return bool(result.is_error), text, result.structured_content

    return asyncio.run(run())


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    """Every test runs as if a token were configured."""
    monkeypatch.setattr(gl, "TOKEN", "test-token")
    monkeypatch.setattr(gl, "GOODLINKS_API", "http://localhost:9428/api/v1")


@pytest.fixture(autouse=True)
def _clear_viewer_cache():
    """The viewer's module-level cache must not leak between tests."""
    viewer._cache["links"] = None
    viewer._cache["at"] = 0.0
    yield
    viewer._cache["links"] = None
    viewer._cache["at"] = 0.0


@pytest.fixture
def client():
    """TestClient for the FastAPI viewer."""
    from fastapi.testclient import TestClient

    return TestClient(viewer.app, raise_server_exceptions=False)
