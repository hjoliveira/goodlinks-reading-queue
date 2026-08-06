# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mcp>=2.0",
#     "httpx>=0.27",
# ]
# ///
"""Read-only MCP server over the local GoodLinks API.

Exposes three tools:

    goodlinks_search_links        full-text search across the library
    goodlinks_list_links          a page of links from a named list
    goodlinks_get_article_content extracted article text for one link

The server is read-only by construction: it only ever issues GET requests, so
nothing it does can modify or delete a link. Requires GoodLinks 3.2+ with the
API enabled (Settings -> API).

Usage:
    uv run --env-file .env goodlinks_mcp.py

Configuration comes from that .env file (copy .env.example), or from the
environment directly. Required:
    GOODLINKS_TOKEN  API token, from GoodLinks Settings -> API

Optional:
    GOODLINKS_API   Base URL of the GoodLinks API (default http://localhost:9428/api/v1)
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

import goodlinks_client as gl

# Well below the API's 1000 ceiling: a tool call that returns a thousand links
# costs more context than it is ever worth. Callers page with `offset`.
DEFAULT_PAGE = 50
MAX_PAGE = 200

# One call should not be able to bury the caller in a book-length article.
DEFAULT_MAX_CHARS = 20_000
MAX_MAX_CHARS = 200_000

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

mcp = MCPServer(
    "goodlinks_mcp",
    version="0.1.0",
    instructions=(
        "Read-only access to the user's GoodLinks reading queue (a local "
        "read-it-later library). Use goodlinks_search_links to find saved "
        "articles by text, goodlinks_list_links to page through a named list "
        "such as unread or starred, and goodlinks_get_article_content to read "
        "the extracted text of a specific article. Nothing here can modify the "
        "library."
    ),
    # The tool list is static, so clients should not re-fetch it every turn.
    cache_hints={"tools/list": CacheHint(ttl_ms=3_600_000, scope="private")},
)


class Link(BaseModel):
    """One saved link.

    Fields below `added_at` are populated only when a tool is called with
    detail="full"; in the default compact mode they are null.
    """

    id: str = Field(description="Link identifier, used by goodlinks_get_article_content")
    title: str | None = Field(default=None, description="Article title")
    url: str = Field(description="The saved URL")
    tags: list[str] = Field(default_factory=list, description="Tags, as full hierarchical paths")
    word_count: int | None = Field(default=None, description="Estimated words in the article")
    reading_minutes: float | None = Field(
        default=None, description=f"Estimated reading time at ~{gl.WORDS_PER_MINUTE} words/minute"
    )
    starred: bool = Field(default=False, description="Whether the link is starred")
    read: bool = Field(default=False, description="Whether the link has been read")
    added_at: str | None = Field(default=None, description="ISO-8601 time the link was saved")

    summary: str | None = Field(default=None, description="Saved summary (detail='full' only)")
    author: str | None = Field(default=None, description="Article author (detail='full' only)")
    read_at: str | None = Field(default=None, description="ISO-8601 read time (detail='full' only)")
    modified_at: str | None = Field(
        default=None, description="ISO-8601 last modification (detail='full' only)"
    )
    highlighted: bool | None = Field(
        default=None, description="Whether the link has highlights (detail='full' only)"
    )


class LinkResults(BaseModel):
    """A page of links, plus everything needed to fetch the next one."""

    links: list[Link]
    count: int = Field(description="Links in this response")
    offset: int = Field(description="Offset this page started at")
    has_more: bool = Field(description="Whether more links match beyond this page")
    next_offset: int | None = Field(
        default=None, description="Pass as `offset` to fetch the next page; null when exhausted"
    )
    list_name: str = Field(description="The list that was queried")
    applied_filters: dict[str, Any] = Field(
        default_factory=dict, description="Filters actually sent to GoodLinks, for verification"
    )


class ArticleContent(BaseModel):
    """Extracted text of one article, possibly a slice of a longer body."""

    link_id: str
    title: str | None = None
    url: str | None = None
    format: str = Field(description="Format the text is in")
    word_count: int | None = Field(default=None, description="Word count from the link's metadata")
    content: str = Field(description="The article text (a slice when `truncated` is true)")
    char_offset: int = Field(description="Character offset this slice starts at")
    chars_returned: int
    total_chars: int = Field(description="Length of the full article text")
    truncated: bool = Field(description="Whether text was withheld to respect max_chars")
    next_char_offset: int | None = Field(
        default=None, description="Pass as `char_offset` to continue reading; null at the end"
    )


def _to_link(raw: dict[str, Any], detail: str) -> Link:
    """Project a GoodLinks API link object into our response model."""
    word_count = raw.get("wordCount")
    link = Link(
        id=str(raw.get("id", "")),
        title=raw.get("title"),
        url=raw.get("url", ""),
        tags=raw.get("tags") or [],
        word_count=word_count if isinstance(word_count, int) else None,
        reading_minutes=gl.reading_minutes(word_count),
        starred=bool(raw.get("starred")),
        read=raw.get("readAt") is not None,
        added_at=raw.get("addedAt"),
    )
    if detail == "full":
        link.summary = raw.get("summary")
        link.author = raw.get("author")
        link.read_at = raw.get("readAt")
        link.modified_at = raw.get("modifiedAt")
        link.highlighted = bool(raw.get("highlighted"))
    return link


async def _query_links(
    list_name: str,
    detail: str,
    limit: int,
    offset: int,
    **filters: Any,
) -> LinkResults:
    """Run one list/search query and shape it into LinkResults.

    Shared by goodlinks_search_links and goodlinks_list_links so pagination and
    projection behave identically in both.
    """
    try:
        raw_links, has_more = await gl.fetch_links(
            list_name, limit=limit, offset=offset, **filters
        )
    except gl.GoodLinksError as exc:
        raise ValueError(str(exc)) from exc

    links = [_to_link(raw, detail) for raw in raw_links]
    return LinkResults(
        links=links,
        count=len(links),
        offset=offset,
        has_more=has_more,
        next_offset=offset + len(links) if has_more and links else None,
        list_name=list_name,
        applied_filters={k: v for k, v in filters.items() if v is not None and v != []},
    )


@mcp.tool(
    name="goodlinks_search_links",
    title="Search GoodLinks",
    annotations=READ_ONLY,
)
async def goodlinks_search_links(
    query: Annotated[
        str,
        Field(
            description=(
                "Text to search for. Matches against title, summary, the article's "
                "full body text, URL, and author."
            ),
            min_length=1,
            max_length=500,
        ),
    ],
    list_name: Annotated[
        Literal["all", "unread", "starred", "read", "tagged", "untagged"],
        Field(description="Restrict the search to one list. Defaults to the whole library."),
    ] = "all",
    tags: Annotated[
        list[str] | None,
        Field(
            description=(
                "Only return links carrying at least ONE of these tags (OR, not AND). "
                "Use full hierarchical paths, e.g. 'technology/programming'."
            )
        ),
    ] = None,
    starred: Annotated[
        bool | None, Field(description="true for starred only, false for unstarred only")
    ] = None,
    read: Annotated[
        bool | None, Field(description="true for read only, false for unread only")
    ] = None,
    highlighted: Annotated[
        bool | None, Field(description="true for links with highlights only")
    ] = None,
    added_after: Annotated[
        str | None, Field(description="Only links saved after this ISO-8601 date/time")
    ] = None,
    added_before: Annotated[
        str | None, Field(description="Only links saved before this ISO-8601 date/time")
    ] = None,
    min_words: Annotated[
        int | None, Field(description="Only articles of at least this many words", ge=0)
    ] = None,
    max_words: Annotated[
        int | None, Field(description="Only articles of at most this many words", ge=0)
    ] = None,
    detail: Annotated[
        Literal["compact", "full"],
        Field(description="'full' adds summary, author, read_at, modified_at, highlighted"),
    ] = "compact",
    limit: Annotated[
        int, Field(description="Maximum links to return", ge=1, le=MAX_PAGE)
    ] = DEFAULT_PAGE,
    offset: Annotated[int, Field(description="Links to skip, for paging", ge=0)] = 0,
) -> LinkResults:
    """Search the user's saved GoodLinks articles by text.

    The search covers the full extracted body of each article, not just its
    metadata, so this answers "what have I saved about X" in one call. Results
    are ordered newest-saved first.

    Use this when the user asks about a topic. Use goodlinks_list_links instead
    when they ask for a whole list ("my unread", "everything starred") with no
    search term.

    Returns LinkResults: `links` (id, title, url, tags, word_count,
    reading_minutes, starred, read, added_at — plus summary/author/read_at/
    modified_at/highlighted when detail='full'), `count`, `offset`, `has_more`,
    `next_offset`, `list_name`, and `applied_filters` echoing the filters sent.

    When `has_more` is true, the result is a partial view of the matches — say
    so rather than presenting it as the complete set, and pass `next_offset`
    back as `offset` to continue.

    Raises a tool error naming the fix when GoodLinks is unreachable or the API
    token is rejected.
    """
    return await _query_links(
        list_name,
        detail,
        limit,
        offset,
        search=query,
        tags=tags,
        starred=starred,
        read=read,
        highlighted=highlighted,
        added_after=added_after,
        added_before=added_before,
        word_count_min=min_words,
        word_count_max=max_words,
    )


@mcp.tool(
    name="goodlinks_list_links",
    title="List GoodLinks by list",
    annotations=READ_ONLY,
)
async def goodlinks_list_links(
    list_name: Annotated[
        Literal["all", "unread", "starred", "read", "tagged", "untagged"],
        Field(
            description=(
                "Which list to read: 'all' (whole library), 'unread', 'starred', "
                "'read', 'tagged' (has at least one tag), 'untagged'."
            )
        ),
    ],
    tags: Annotated[
        list[str] | None,
        Field(
            description=(
                "Only return links carrying at least ONE of these tags (OR, not AND). "
                "Ignored when list_name is 'untagged'."
            )
        ),
    ] = None,
    include_read: Annotated[
        bool | None,
        Field(
            description=(
                "Include already-read links. Only affects the 'starred' and "
                "'untagged' lists, where GoodLinks excludes read links by default."
            )
        ),
    ] = None,
    added_after: Annotated[
        str | None, Field(description="Only links saved after this ISO-8601 date/time")
    ] = None,
    added_before: Annotated[
        str | None, Field(description="Only links saved before this ISO-8601 date/time")
    ] = None,
    min_words: Annotated[
        int | None, Field(description="Only articles of at least this many words", ge=0)
    ] = None,
    max_words: Annotated[
        int | None, Field(description="Only articles of at most this many words", ge=0)
    ] = None,
    detail: Annotated[
        Literal["compact", "full"],
        Field(description="'full' adds summary, author, read_at, modified_at, highlighted"),
    ] = "compact",
    limit: Annotated[
        int, Field(description="Maximum links to return", ge=1, le=MAX_PAGE)
    ] = DEFAULT_PAGE,
    offset: Annotated[int, Field(description="Links to skip, for paging", ge=0)] = 0,
) -> LinkResults:
    """Retrieve the links in one of the user's GoodLinks lists.

    Returns links newest-saved first. This is the tool for "show me my unread
    queue" or "what's starred"; use goodlinks_search_links when there is a
    topic or search term involved.

    A library can hold far more links than belong in one response, so a single
    call returns at most `limit` (default 50, max 200) and reports `has_more`.
    To walk an entire list, keep calling with the returned `next_offset` until
    `has_more` is false. Filtering — by tag, date, or word count — is almost
    always better than paging through everything.

    Returns LinkResults, the same shape as goodlinks_search_links: `links`,
    `count`, `offset`, `has_more`, `next_offset`, `list_name`,
    `applied_filters`.

    Note that `min_words`/`max_words` drop links whose word count GoodLinks has
    not computed, and that `reading_minutes` on each link is an estimate at
    ~230 words/minute, not a measured value.

    Raises a tool error naming the fix when GoodLinks is unreachable or the API
    token is rejected.
    """
    return await _query_links(
        list_name,
        detail,
        limit,
        offset,
        tags=tags,
        include_read=include_read,
        added_after=added_after,
        added_before=added_before,
        word_count_min=min_words,
        word_count_max=max_words,
    )


@mcp.tool(
    name="goodlinks_get_article_content",
    title="Read a saved article",
    annotations=READ_ONLY,
)
async def goodlinks_get_article_content(
    link_id: Annotated[
        str,
        Field(
            description="Link id, as returned in the `id` field by the search or list tools",
            min_length=1,
        ),
    ],
    format: Annotated[
        Literal["markdown", "plaintext", "html"],
        Field(description="Text format. 'markdown' keeps structure and reads well; "
                          "'plaintext' is smallest; 'html' preserves original markup."),
    ] = "markdown",
    max_chars: Annotated[
        int,
        Field(
            description="Maximum characters of article text to return in one call",
            ge=500,
            le=MAX_MAX_CHARS,
        ),
    ] = DEFAULT_MAX_CHARS,
    char_offset: Annotated[
        int,
        Field(description="Character offset to start from, for reading a long article in parts", ge=0),
    ] = 0,
) -> ArticleContent:
    """Read the text GoodLinks extracted from a saved article.

    This returns the copy already stored in the user's library, so it works
    without re-fetching the page from the web — which also means it reflects
    the article as it was when saved, and is unavailable for links GoodLinks
    never extracted content for.

    Get `link_id` from goodlinks_search_links or goodlinks_list_links first.

    Long articles are returned in slices: at most `max_chars` characters
    (default 20000) starting at `char_offset`. When `truncated` is true, more
    text remains — call again with `next_char_offset` as `char_offset` if the
    task genuinely needs the rest, since most summarisation questions are
    answerable from the first slice.

    Returns ArticleContent: `content` (the text), `format`, `link_id`, `title`,
    `url`, `word_count`, `char_offset`, `chars_returned`, `total_chars`,
    `truncated`, `next_char_offset`.

    Raises a tool error naming the fix when GoodLinks is unreachable, the token
    is rejected, the link id does not exist, or the article has no stored text.
    """
    try:
        text = await gl.fetch_content(link_id, format)
    except gl.GoodLinksError as exc:
        raise ValueError(str(exc)) from exc

    # Metadata is a cheap local call and makes the returned text identifiable;
    # a missing link would already have failed above, so tolerate a miss here.
    title: str | None = None
    url: str | None = None
    word_count: int | None = None
    try:
        meta = await gl.fetch_link(link_id)
        title = meta.get("title")
        url = meta.get("url")
        raw_count = meta.get("wordCount")
        word_count = raw_count if isinstance(raw_count, int) else None
    except gl.GoodLinksError:
        pass

    total = len(text)
    if char_offset >= total and total > 0:
        raise ValueError(
            f"char_offset {char_offset} is past the end of the article ({total} characters)."
        )
    if not text.strip():
        raise ValueError(
            f"GoodLinks has no extracted article text for link {link_id}. It may be a "
            "bookmark saved without content, or extraction may have failed for this page."
        )

    slice_ = text[char_offset : char_offset + max_chars]
    end = char_offset + len(slice_)
    truncated = end < total

    return ArticleContent(
        link_id=link_id,
        title=title,
        url=url,
        format=format,
        word_count=word_count,
        content=slice_,
        char_offset=char_offset,
        chars_returned=len(slice_),
        total_chars=total,
        truncated=truncated,
        next_char_offset=end if truncated else None,
    )


if __name__ == "__main__":
    import os

    if not gl.TOKEN:
        raise SystemExit(
            "GOODLINKS_TOKEN is not set. Put it in .env (copy .env.example) and "
            "start with: uv run --env-file .env <script>. The token is in "
            "GoodLinks under Settings -> API."
        )

    # stdio suits a local server and is what most clients launch. The
    # streamable-http option exists because the 2026-07-28 revision is only
    # reachable over HTTP — the initialize handshake tops out at 2025-11-25 —
    # so the cache hints above take effect only on that transport. Bind
    # loopback: this serves the user's whole reading history.
    if os.environ.get("MCP_TRANSPORT") == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8301")),
            stateless_http=True,
        )
    else:
        mcp.run()
