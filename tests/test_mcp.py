"""Unit tests for goodlinks_mcp: projection, pagination, and the tool surface."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import goodlinks_mcp as mcp_srv
from conftest import call_tool, json_route, links_page, mock_api


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
