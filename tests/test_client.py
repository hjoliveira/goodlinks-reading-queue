"""Unit tests for goodlinks_client: request plumbing and error translation."""

from __future__ import annotations

import asyncio

import httpx
import pytest

import goodlinks_client as gl
from conftest import json_route, links_page, mock_api


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
