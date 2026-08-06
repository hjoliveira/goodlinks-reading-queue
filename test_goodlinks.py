"""Unit tests for the GoodLinks client, MCP server, and web viewer.

Run with:
    uv run test_goodlinks.py

No GoodLinks instance and no network are needed: every HTTP call is served by
an httpx MockTransport, so the tests are hermetic and fast.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from typing import Any, Callable
from unittest import mock

import httpx
import pytest

import goodlinks_client as gl
import goodlinks_mcp as mcp_srv
import goodlinks_server as viewer
from mcp.server.mcpserver.exceptions import ToolError

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    """Every test runs as if a token were configured."""
    monkeypatch.setattr(gl, "TOKEN", "test-token")
    monkeypatch.setattr(gl, "GOODLINKS_API", "http://localhost:9428/api/v1")


@pytest.fixture(autouse=True)
def _clear_viewer_cache():
    viewer._cache["links"] = None
    viewer._cache["at"] = 0.0
    yield
    viewer._cache["links"] = None
    viewer._cache["at"] = 0.0


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


# --------------------------------------------------------------------------
# goodlinks_client: request plumbing and error translation
# --------------------------------------------------------------------------


class TestRequestErrors:
    def test_missing_token_fails_before_any_request(self, monkeypatch):
        monkeypatch.setattr(gl, "TOKEN", "")
        called = []

        with mock_api(json_route({}, record=called)):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.get_json("/tags"))

        assert "GOODLINKS_TOKEN" in str(exc.value)
        assert "Settings -> API" in str(exc.value)
        assert not called, "should not hit the network without a token"

    def test_sends_bearer_token(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/tags": []}, record=seen)):
            asyncio.run(gl.get_json("/tags"))
        assert seen[0].headers["Authorization"] == "Bearer test-token"

    def test_401_names_the_env_var(self):
        with mock_api(lambda r: httpx.Response(401, json={"error": "nope"})):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.get_json("/tags"))
        assert "401" in str(exc.value)
        assert "GOODLINKS_TOKEN" in str(exc.value)

    def test_404_default_message(self):
        with mock_api(lambda r: httpx.Response(404, json={})):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.get_json("/links/nope"))
        assert "Not found" in str(exc.value)

    def test_404_custom_message_wins(self):
        with mock_api(lambda r: httpx.Response(404, json={})):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.request("/links/x/content", not_found="no article text here"))
        assert str(exc.value) == "no article text here"

    def test_400_includes_the_server_explanation(self):
        with mock_api(lambda r: httpx.Response(400, text="limit must be <= 1000")):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.get_json("/lists/all"))
        assert "limit must be <= 1000" in str(exc.value)

    def test_500_reports_the_status(self):
        with mock_api(lambda r: httpx.Response(503, text="down")):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.get_json("/tags"))
        assert "503" in str(exc.value)

    def test_connect_error_tells_you_what_to_check(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)

        with mock_api(boom):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.get_json("/tags"))
        message = str(exc.value)
        assert "Is GoodLinks running" in message
        assert "Settings -> API" in message
        assert gl.GOODLINKS_API in message

    def test_timeout_is_distinct_from_connect_failure(self):
        def slow(request):
            raise httpx.TimeoutException("too slow", request=request)

        with mock_api(slow):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.get_json("/tags"))
        assert "did not respond" in str(exc.value)

    def test_non_json_body_is_reported_clearly(self):
        with mock_api(lambda r: httpx.Response(200, text="<html>nope</html>")):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.get_json("/tags"))
        assert "non-JSON" in str(exc.value)


class TestQueryParams:
    def test_unset_filters_are_omitted(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            asyncio.run(gl.fetch_links("all", limit=10))
        params = dict(seen[0].url.params)
        assert set(params) == {"limit", "offset"}

    def test_tags_serialise_as_repeated_tag_params(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            asyncio.run(gl.fetch_links("all", limit=10, tags=["a/b", "c"]))
        assert seen[0].url.params.get_list("tag") == ["a/b", "c"]

    def test_false_is_a_filter_not_an_absence(self):
        """`starred=False` means unstarred-only and must reach the API."""
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            asyncio.run(gl.fetch_links("all", limit=10, starred=False, read=True))
        params = dict(seen[0].url.params)
        assert params["starred"] == "false"
        assert params["read"] == "true"

    def test_empty_tag_list_is_dropped(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            asyncio.run(gl.fetch_links("all", limit=10, tags=[]))
        assert "tag" not in dict(seen[0].url.params)

    def test_filters_map_to_api_spellings(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/unread": links_page(1)}, record=seen)):
            asyncio.run(
                gl.fetch_links(
                    "unread",
                    limit=5,
                    offset=10,
                    search="rust",
                    include_read=True,
                    added_after="2026-01-01",
                    word_count_min=100,
                    word_count_max=900,
                )
            )
        params = dict(seen[0].url.params)
        assert params["search"] == "rust"
        assert params["includeRead"] == "true"
        assert params["addedAfter"] == "2026-01-01"
        assert params["wordCountMin"] == "100"
        assert params["wordCountMax"] == "900"
        assert params["offset"] == "10"

    def test_unknown_list_is_rejected_without_a_request(self):
        called: list[httpx.Request] = []
        with mock_api(json_route({}, record=called)):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.fetch_links("archive", limit=5))
        assert "archive" in str(exc.value)
        assert "unread" in str(exc.value), "should list the valid names"
        assert not called


class TestFetchHelpers:
    def test_fetch_links_returns_page_and_flag(self):
        with mock_api(json_route({"/lists/all": links_page(3, has_more=True)})):
            links, has_more = asyncio.run(gl.fetch_links("all", limit=3))
        assert [x["id"] for x in links] == ["id000", "id001", "id002"]
        assert has_more is True

    def test_fetch_links_tolerates_missing_data_key(self):
        with mock_api(json_route({"/lists/all": {"hasMore": False}})):
            links, has_more = asyncio.run(gl.fetch_links("all", limit=3))
        assert links == []
        assert has_more is False

    def test_fetch_links_rejects_non_object_payload(self):
        with mock_api(json_route({"/lists/all": ["not", "an", "object"]})):
            with pytest.raises(gl.GoodLinksError):
                asyncio.run(gl.fetch_links("all", limit=3))

    def test_fetch_link_accepts_bare_object(self):
        with mock_api(json_route({"/links/id1": {"id": "id1", "title": "T"}})):
            link = asyncio.run(gl.fetch_link("id1"))
        assert link["title"] == "T"

    def test_fetch_link_unwraps_data_envelope(self):
        with mock_api(json_route({"/links/id1": {"data": {"id": "id1", "title": "Wrapped"}}})):
            link = asyncio.run(gl.fetch_link("id1"))
        assert link["title"] == "Wrapped"


class TestFetchContent:
    def test_plain_text_body_passes_through(self):
        body = "# Heading\n\nBody text."
        with mock_api(
            lambda r: httpx.Response(200, text=body, headers={"content-type": "text/plain"})
        ):
            assert asyncio.run(gl.fetch_content("id1", "markdown")) == body

    def test_format_reaches_the_api(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="x", headers={"content-type": "text/plain"})

        with mock_api(handler):
            asyncio.run(gl.fetch_content("id1", "plaintext"))
        assert dict(seen[0].url.params)["format"] == "plaintext"
        assert seen[0].url.path.endswith("/links/id1/content")

    @pytest.mark.parametrize("key", ["content", "markdownContent", "text", "html", "data"])
    def test_json_envelope_is_unwrapped(self, key):
        with mock_api(lambda r: httpx.Response(200, json={key: "the article"})):
            assert asyncio.run(gl.fetch_content("id1", "markdown")) == "the article"

    def test_bare_json_string_is_accepted(self):
        with mock_api(lambda r: httpx.Response(200, json="the article")):
            assert asyncio.run(gl.fetch_content("id1", "markdown")) == "the article"

    def test_unrecognised_json_shape_raises(self):
        with mock_api(lambda r: httpx.Response(200, json={"surprise": 1})):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.fetch_content("id1", "markdown"))
        assert "id1" in str(exc.value)

    def test_404_blames_content_not_the_link(self):
        with mock_api(lambda r: httpx.Response(404, json={})):
            with pytest.raises(gl.GoodLinksError) as exc:
                asyncio.run(gl.fetch_content("id1", "markdown"))
        assert "No stored article content" in str(exc.value)
        assert "deleted" not in str(exc.value)


class TestReadingMinutes:
    @pytest.mark.parametrize("bad", [None, 0, -5, "460", 12.5])
    def test_non_positive_ints_have_no_estimate(self, bad):
        assert gl.reading_minutes(bad) is None

    def test_rounds_to_one_decimal(self):
        assert gl.reading_minutes(gl.WORDS_PER_MINUTE) == 1.0
        assert gl.reading_minutes(gl.WORDS_PER_MINUTE * 2) == 2.0
        assert gl.reading_minutes(100) == round(100 / gl.WORDS_PER_MINUTE, 1)


# --------------------------------------------------------------------------
# goodlinks_mcp: projection, pagination, tool surface
# --------------------------------------------------------------------------


class TestLinkProjection:
    def test_compact_omits_detail_fields(self):
        raw = links_page(1)["data"][0]
        link = mcp_srv._to_link(raw, "compact")
        assert link.title == "Article 0"
        assert link.summary is None
        assert link.author is None
        assert link.highlighted is None

    def test_full_includes_detail_fields(self):
        raw = links_page(2)["data"][1]
        link = mcp_srv._to_link(raw, "full")
        assert link.summary == "Summary 1"
        assert link.author == "Ada"
        assert link.modified_at == "2026-08-02T10:00:00Z"
        assert link.highlighted is False

    def test_read_is_derived_from_read_at(self):
        unread = mcp_srv._to_link({"id": "a", "url": "u", "readAt": None}, "compact")
        read = mcp_srv._to_link({"id": "b", "url": "u", "readAt": "2026-01-01"}, "compact")
        assert unread.read is False
        assert read.read is True

    def test_missing_fields_do_not_crash(self):
        link = mcp_srv._to_link({}, "full")
        assert link.id == ""
        assert link.tags == []
        assert link.word_count is None
        assert link.reading_minutes is None

    def test_null_word_count_yields_no_estimate(self):
        link = mcp_srv._to_link({"id": "a", "url": "u", "wordCount": None}, "compact")
        assert link.reading_minutes is None


class TestPagination:
    def _results(self, count, has_more, offset=0):
        with mock_api(json_route({"/lists/all": links_page(count, has_more)})):
            return asyncio.run(mcp_srv._query_links("all", "compact", 50, offset))

    def test_next_offset_advances_by_returned_count(self):
        r = self._results(10, True, offset=20)
        assert r.has_more is True
        assert r.next_offset == 30

    def test_no_next_offset_on_the_last_page(self):
        r = self._results(10, False)
        assert r.has_more is False
        assert r.next_offset is None

    def test_empty_page_never_produces_a_next_offset(self):
        """has_more with no rows would otherwise loop a caller forever."""
        r = self._results(0, True)
        assert r.next_offset is None

    def test_applied_filters_echo_only_what_was_sent(self):
        with mock_api(json_route({"/lists/all": links_page(1)})):
            r = asyncio.run(
                mcp_srv._query_links(
                    "all", "compact", 10, 0, search="rust", tags=None, starred=True
                )
            )
        assert r.applied_filters == {"search": "rust", "starred": True}

    def test_api_errors_become_tool_errors(self):
        with mock_api(lambda r: httpx.Response(401, json={})):
            with pytest.raises(ValueError) as exc:
                asyncio.run(mcp_srv._query_links("all", "compact", 10, 0))
        assert "GOODLINKS_TOKEN" in str(exc.value)


class TestToolSurface:
    def test_exactly_three_tools(self):
        names = {t.name for t in asyncio.run(mcp_srv.mcp.list_tools())}
        assert names == {
            "goodlinks_search_links",
            "goodlinks_list_links",
            "goodlinks_get_article_content",
        }

    def test_every_tool_is_annotated_read_only(self):
        for tool in asyncio.run(mcp_srv.mcp.list_tools()):
            assert tool.annotations.read_only_hint is True, tool.name
            assert tool.annotations.destructive_hint is False, tool.name

    def test_every_tool_documents_itself(self):
        for tool in asyncio.run(mcp_srv.mcp.list_tools()):
            assert tool.description and len(tool.description) > 100, tool.name
            assert tool.output_schema, tool.name

    def test_required_arguments(self):
        required = {
            t.name: set(t.input_schema.get("required", []))
            for t in asyncio.run(mcp_srv.mcp.list_tools())
        }
        assert required["goodlinks_search_links"] == {"query"}
        assert required["goodlinks_list_links"] == {"list_name"}
        assert required["goodlinks_get_article_content"] == {"link_id"}

    def test_tools_list_carries_a_cache_hint(self):
        """The static tool list should tell clients not to re-fetch it hourly.

        Reaches into a private attribute because that is the only in-process
        place the hint is visible: it reaches the wire as `ttlMs`/`cacheScope`
        only when a client negotiates 2026-07-28, which needs the HTTP
        transport. This still catches the hint being dropped from the
        constructor.
        """
        hint = mcp_srv.mcp._lowlevel_server.cache_hints["tools/list"]
        assert hint.ttl_ms == 3_600_000
        assert hint.scope == "private"


class TestSearchAndListTools:
    def test_search_returns_structured_results(self):
        with mock_api(json_route({"/lists/all": links_page(2, has_more=True)})):
            is_error, _, data = call_tool("goodlinks_search_links", {"query": "rust", "limit": 2})
        assert is_error is False
        assert data["count"] == 2
        assert data["has_more"] is True
        assert data["next_offset"] == 2
        assert data["links"][0]["title"] == "Article 0"

    def test_search_sends_the_query_as_search(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            call_tool("goodlinks_search_links", {"query": "rust"})
        assert dict(seen[0].url.params)["search"] == "rust"

    def test_list_targets_the_named_list(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/starred": links_page(1)}, record=seen)):
            is_error, _, data = call_tool("goodlinks_list_links", {"list_name": "starred"})
        assert is_error is False
        assert seen[0].url.path.endswith("/lists/starred")
        assert data["list_name"] == "starred"

    def test_default_page_size(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route({"/lists/all": links_page(1)}, record=seen)):
            call_tool("goodlinks_list_links", {"list_name": "all"})
        assert dict(seen[0].url.params)["limit"] == str(mcp_srv.DEFAULT_PAGE)

    @pytest.mark.parametrize("limit", [0, mcp_srv.MAX_PAGE + 1, 5000])
    def test_limit_outside_the_range_is_rejected(self, limit):
        is_error, message, _ = call_tool(
            "goodlinks_list_links", {"list_name": "all", "limit": limit}
        )
        assert is_error is True
        assert "limit" in message

    def test_unknown_list_name_is_rejected_by_the_schema(self):
        is_error, _, _ = call_tool("goodlinks_list_links", {"list_name": "archive"})
        assert is_error is True

    def test_negative_offset_is_rejected(self):
        is_error, _, _ = call_tool("goodlinks_list_links", {"list_name": "all", "offset": -1})
        assert is_error is True

    def test_unreachable_api_surfaces_the_actionable_message(self):
        def boom(request):
            raise httpx.ConnectError("refused", request=request)

        with mock_api(boom):
            is_error, message, _ = call_tool("goodlinks_list_links", {"list_name": "all"})
        assert is_error is True
        assert "Is GoodLinks running" in message


class TestArticleTool:
    ARTICLE = "word " * 2000  # 10000 characters

    def _routes(self, text=None):
        text = self.ARTICLE if text is None else text
        return {
            "/links/id1/content": httpx.Response(
                200, text=text, headers={"content-type": "text/plain"}
            ),
            "/links/id1": {"id": "id1", "title": "A Title", "url": "https://x", "wordCount": 2000},
        }

    def test_short_article_returns_whole(self):
        with mock_api(json_route(self._routes("short body"))):
            is_error, _, data = call_tool("goodlinks_get_article_content", {"link_id": "id1"})
        assert is_error is False
        assert data["content"] == "short body"
        assert data["truncated"] is False
        assert data["next_char_offset"] is None
        assert data["total_chars"] == 10

    def test_long_article_is_sliced_not_cut_off(self):
        with mock_api(json_route(self._routes())):
            is_error, _, data = call_tool(
                "goodlinks_get_article_content", {"link_id": "id1", "max_chars": 1000}
            )
        assert is_error is False
        assert data["chars_returned"] == 1000
        assert data["total_chars"] == 10000
        assert data["truncated"] is True
        assert data["next_char_offset"] == 1000

    def test_offset_continues_where_the_slice_ended(self):
        with mock_api(json_route(self._routes())):
            _, _, first = call_tool(
                "goodlinks_get_article_content", {"link_id": "id1", "max_chars": 1000}
            )
            _, _, second = call_tool(
                "goodlinks_get_article_content",
                {"link_id": "id1", "max_chars": 1000, "char_offset": first["next_char_offset"]},
            )
        assert second["char_offset"] == 1000
        assert first["content"] + second["content"] == self.ARTICLE[:2000]

    def test_final_slice_reports_completion(self):
        with mock_api(json_route(self._routes())):
            _, _, data = call_tool(
                "goodlinks_get_article_content",
                {"link_id": "id1", "max_chars": 1000, "char_offset": 9000},
            )
        assert data["truncated"] is False
        assert data["next_char_offset"] is None

    def test_metadata_travels_with_the_text(self):
        with mock_api(json_route(self._routes())):
            _, _, data = call_tool("goodlinks_get_article_content", {"link_id": "id1"})
        assert data["title"] == "A Title"
        assert data["url"] == "https://x"
        assert data["word_count"] == 2000

    def test_text_still_returned_when_metadata_lookup_fails(self):
        routes = {
            "/links/id1/content": httpx.Response(
                200, text="body", headers={"content-type": "text/plain"}
            )
            # no /links/id1 route: metadata 404s
        }
        with mock_api(json_route(routes)):
            is_error, _, data = call_tool("goodlinks_get_article_content", {"link_id": "id1"})
        assert is_error is False
        assert data["content"] == "body"
        assert data["title"] is None

    def test_offset_past_the_end_is_an_error(self):
        with mock_api(json_route(self._routes())):
            is_error, message, _ = call_tool(
                "goodlinks_get_article_content", {"link_id": "id1", "char_offset": 99999}
            )
        assert is_error is True
        assert "past the end" in message

    def test_blank_content_explains_why(self):
        with mock_api(json_route(self._routes("   \n  "))):
            is_error, message, _ = call_tool("goodlinks_get_article_content", {"link_id": "id1"})
        assert is_error is True
        assert "no extracted article text" in message

    def test_default_format_is_markdown(self):
        seen: list[httpx.Request] = []
        with mock_api(json_route(self._routes(), record=seen)):
            call_tool("goodlinks_get_article_content", {"link_id": "id1"})
        assert dict(seen[0].url.params)["format"] == "markdown"

    @pytest.mark.parametrize("fmt", ["markdown", "plaintext", "html"])
    def test_supported_formats_pass_through(self, fmt):
        seen: list[httpx.Request] = []
        with mock_api(json_route(self._routes(), record=seen)):
            is_error, _, data = call_tool(
                "goodlinks_get_article_content", {"link_id": "id1", "format": fmt}
            )
        assert is_error is False
        assert data["format"] == fmt
        assert dict(seen[0].url.params)["format"] == fmt

    def test_unsupported_format_is_rejected(self):
        is_error, _, _ = call_tool(
            "goodlinks_get_article_content", {"link_id": "id1", "format": "pdf"}
        )
        assert is_error is True

    def test_max_chars_below_the_floor_is_rejected(self):
        is_error, _, _ = call_tool(
            "goodlinks_get_article_content", {"link_id": "id1", "max_chars": 10}
        )
        assert is_error is True


# --------------------------------------------------------------------------
# goodlinks_server: the web viewer
# --------------------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(viewer.app, raise_server_exceptions=False)


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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
