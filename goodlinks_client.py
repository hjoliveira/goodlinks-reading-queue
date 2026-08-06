"""Shared async client for the local GoodLinks HTTP API.

GoodLinks 3.2+ runs a web server on the same machine (default
http://localhost:9428/api/v1), enabled in Settings -> API. Every request
carries a bearer token, also shown in Settings -> API.

This module owns the two things that are easy to get wrong when talking to
that API — the auth header and the failure messages — so callers only deal in
plain dicts and one exception type.

Environment (supplied via `uv run --env-file .env`, or set directly):
    GOODLINKS_TOKEN  API token (required)
    GOODLINKS_API    Base URL (default http://localhost:9428/api/v1)
"""

from __future__ import annotations

import os
from typing import Any, Literal

import httpx

GOODLINKS_API = os.environ.get("GOODLINKS_API", "http://localhost:9428/api/v1")
TOKEN = os.environ.get("GOODLINKS_TOKEN", "")

# The named lists exposed at /lists/{list}.
ListName = Literal["all", "unread", "starred", "read", "tagged", "untagged"]
LIST_NAMES: tuple[str, ...] = ("all", "unread", "starred", "read", "tagged", "untagged")

# The API accepts at most 1000 links per page.
API_PAGE_MAX = 1000

# Used only to turn wordCount into an estimate; not a fact about the reader.
WORDS_PER_MINUTE = 230

TIMEOUT = 30.0


class GoodLinksError(RuntimeError):
    """A GoodLinks API call failed, with a message worth showing to a human."""


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _drop_none(params: dict[str, Any]) -> dict[str, Any]:
    """Strip unset filters so we never send `starred=None` as a query string."""
    return {k: v for k, v in params.items() if v is not None and v != []}


async def request(
    path: str,
    params: dict[str, Any] | None = None,
    *,
    not_found: str | None = None,
) -> httpx.Response:
    """GET `path` under the API base and translate failures into GoodLinksError.

    The three failures that actually happen in practice — GoodLinks not
    running, API switch off, wrong token — all get a message that names the fix.
    `not_found` overrides the 404 text, since what a missing resource means
    depends on which endpoint was asked.
    """
    if not TOKEN:
        raise GoodLinksError(
            "GOODLINKS_TOKEN is not set. Copy the token from GoodLinks -> "
            "Settings -> API and set it in the environment."
        )

    url = f"{GOODLINKS_API}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(url, params=_drop_none(params or {}), headers=_headers())
    except httpx.ConnectError as exc:
        raise GoodLinksError(
            f"Could not reach the GoodLinks API at {GOODLINKS_API}. Is GoodLinks "
            "running on this machine, and is the API enabled in Settings -> API?"
        ) from exc
    except httpx.TimeoutException as exc:
        raise GoodLinksError(f"The GoodLinks API did not respond within {TIMEOUT:.0f}s.") from exc
    except httpx.HTTPError as exc:
        raise GoodLinksError(f"Request to the GoodLinks API failed: {exc}") from exc

    if resp.status_code == 401:
        raise GoodLinksError(
            "GoodLinks rejected the API token (401). Check GOODLINKS_TOKEN against "
            "Settings -> API."
        )
    if resp.status_code == 404:
        raise GoodLinksError(
            not_found or f"Not found: {path}. The link may have been deleted."
        )
    if resp.status_code == 400:
        raise GoodLinksError(f"GoodLinks rejected the request (400): {resp.text.strip()[:300]}")
    if resp.status_code >= 400:
        raise GoodLinksError(
            f"GoodLinks API returned {resp.status_code} for {path}: {resp.text.strip()[:300]}"
        )
    return resp


async def get_json(path: str, params: dict[str, Any] | None = None) -> Any:
    resp = await request(path, params)
    try:
        return resp.json()
    except ValueError as exc:
        raise GoodLinksError(f"GoodLinks returned a non-JSON response for {path}.") from exc


async def fetch_links(
    list_name: str,
    *,
    limit: int,
    offset: int = 0,
    search: str | None = None,
    tags: list[str] | None = None,
    starred: bool | None = None,
    read: bool | None = None,
    highlighted: bool | None = None,
    include_read: bool | None = None,
    added_after: str | None = None,
    added_before: str | None = None,
    word_count_min: int | None = None,
    word_count_max: int | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Fetch one page from /lists/{list_name}. Returns (links, has_more).

    `limit` is passed straight through and must be within the API's 1..1000
    range; callers are expected to clamp it. Paging past 1000 is the caller's
    job via `offset`, which keeps the number of links crossing this boundary
    something the caller chose rather than something the library decided.
    """
    if list_name not in LIST_NAMES:
        raise GoodLinksError(f"Unknown list {list_name!r}. Valid lists: {', '.join(LIST_NAMES)}.")

    payload = await get_json(
        f"/lists/{list_name}",
        {
            "limit": limit,
            "offset": offset,
            "search": search,
            "tag": tags,  # repeatable; matches links carrying ANY of the tags
            "starred": starred,
            "read": read,
            "highlighted": highlighted,
            "includeRead": include_read,
            "addedAfter": added_after,
            "addedBefore": added_before,
            "wordCountMin": word_count_min,
            "wordCountMax": word_count_max,
        },
    )
    if not isinstance(payload, dict):
        raise GoodLinksError("Unexpected response shape from /lists — expected an object.")
    return payload.get("data") or [], bool(payload.get("hasMore"))


async def fetch_link(link_id: str) -> dict[str, Any]:
    """Fetch a single link's metadata from /links/{id}."""
    payload = await get_json(f"/links/{link_id}")
    # The endpoint has been seen returning the object directly and wrapped in
    # `data`; accept either rather than breaking on a shape we can handle.
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    if isinstance(payload, dict):
        return payload
    raise GoodLinksError(f"Unexpected response shape for link {link_id}.")


async def fetch_content(link_id: str, fmt: str) -> str:
    """Fetch extracted article text from /links/{id}/content.

    Returns the body as a string. The endpoint may serve the text directly or
    wrap it in JSON, so both are handled — a plain-text response is used as-is,
    and a JSON object is searched for the usual content keys.
    """
    resp = await request(
        f"/links/{link_id}/content",
        {"format": fmt},
        not_found=(
            f"No stored article content for link {link_id}. Either the link id does "
            "not exist, or GoodLinks never extracted text for that page."
        ),
    )
    content_type = resp.headers.get("content-type", "")

    if "json" in content_type:
        payload = resp.json()
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            for key in ("content", "markdownContent", "text", "html", "data"):
                value = payload.get(key)
                if isinstance(value, str):
                    return value
        raise GoodLinksError(
            f"Could not find article text in the response for link {link_id}."
        )

    return resp.text


def reading_minutes(word_count: Any) -> float | None:
    """Estimate reading time from a word count, at WORDS_PER_MINUTE."""
    if not isinstance(word_count, int) or word_count <= 0:
        return None
    return round(word_count / WORDS_PER_MINUTE, 1)
