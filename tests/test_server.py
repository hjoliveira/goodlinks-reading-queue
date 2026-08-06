"""Unit tests for goodlinks_server: the web viewer's paging, caching, and errors."""

from __future__ import annotations

import asyncio

import httpx

import goodlinks_client as gl
import goodlinks_server as viewer
from conftest import json_route, links_page, mock_api


class TestFetchAllLinks:
    def test_single_page_library(self):
        with mock_api(json_route({"/lists/all": links_page(5)})):
            links = asyncio.run(viewer.fetch_all_links())
        assert len(links) == 5

    def test_pages_until_exhausted(self):
        pages = [links_page(3, True, 0), links_page(3, True, 3), links_page(2, False, 6)]
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=pages[len(seen) - 1])

        with mock_api(handler):
            links = asyncio.run(viewer.fetch_all_links())

        assert [x["id"] for x in links] == [f"id{i:03d}" for i in range(8)]
        assert len(links) == len(set(x["id"] for x in links))
        assert [dict(r.url.params)["offset"] for r in seen] == ["0", "3", "6"]

    def test_offset_follows_actual_rows_not_requested_limit(self):
        """A short page with hasMore must not cause skipped links."""
        pages = [links_page(2, True, 0), links_page(2, False, 2)]
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=pages[len(seen) - 1])

        with mock_api(handler):
            links = asyncio.run(viewer.fetch_all_links())

        assert dict(seen[1].url.params)["offset"] == "2"
        assert [x["id"] for x in links] == ["id000", "id001", "id002", "id003"]

    def test_empty_page_with_has_more_terminates(self):
        """Guards against an infinite loop if the API ever misreports hasMore.

        The circuit breaker lives in the handler rather than in an
        asyncio.wait_for timeout: MockTransport resolves synchronously, so a
        runaway loop never suspends and a cancellation could never be
        delivered. Failing the request is the only way to break out.
        """
        calls: list[httpx.Request] = []

        def handler(request):
            calls.append(request)
            if len(calls) > 20:
                raise AssertionError("fetch_all_links kept paging past an empty page")
            return httpx.Response(200, json={"data": [], "hasMore": True})

        with mock_api(handler):
            links = asyncio.run(viewer.fetch_all_links())

        assert links == []
        assert len(calls) == 1, "an empty page should end paging immediately"

    def test_requests_the_maximum_page_size(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            asyncio.run(viewer.fetch_all_links())
        assert dict(seen[0].url.params)["limit"] == str(gl.API_PAGE_MAX)


class TestViewerRoutes:
    def test_api_links_returns_the_library(self, client):
        with mock_api(json_route({"/lists/all": links_page(4)})):
            response = client.get("/api/links")
        assert response.status_code == 200
        assert len(response.json()["links"]) == 4

    def test_raw_api_shape_is_preserved_for_the_front_end(self, client):
        with mock_api(json_route({"/lists/all": links_page(1)})):
            link = client.get("/api/links").json()["links"][0]
        assert {"id", "url", "title", "wordCount", "readAt", "starred"} <= set(link)

    def test_second_call_is_served_from_cache(self, client):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            client.get("/api/links")
            client.get("/api/links")
        assert len(seen) == 1

    def test_refresh_bypasses_the_cache(self, client):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            client.get("/api/links")
            client.get("/api/links?refresh=true")
        assert len(seen) == 2

    def test_stale_cache_is_refetched(self, client):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            client.get("/api/links")
            viewer._cache["at"] -= viewer.CACHE_TTL + 1
            client.get("/api/links")
        assert len(seen) == 2

    def test_bad_token_becomes_502_with_the_reason(self, client):
        with mock_api(lambda r: httpx.Response(401, json={})):
            response = client.get("/api/links")
        assert response.status_code == 502
        assert "GOODLINKS_TOKEN" in response.json()["detail"]

    def test_unreachable_api_becomes_502_with_the_reason(self, client):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)

        with mock_api(boom):
            response = client.get("/api/links")
        assert response.status_code == 502
        assert "Is GoodLinks running" in response.json()["detail"]

    def test_failure_does_not_poison_the_cache(self, client):
        with mock_api(lambda r: httpx.Response(401, json={})):
            client.get("/api/links")
        with mock_api(json_route({"/lists/all": links_page(2)})):
            response = client.get("/api/links")
        assert response.status_code == 200
        assert len(response.json()["links"]) == 2

    def test_index_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "<!DOCTYPE html>" in response.text

    def test_service_worker_is_served_as_javascript(self, client):
        response = client.get("/sw.js")
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
