"""Export all GoodLinks links to a Netscape bookmark file for Karakeep.

Run on the Mac where GoodLinks runs, with the same .env as the other scripts:

    uv run --env-file .env goodlinks_to_karakeep.py -o goodlinks.html

Talks to GoodLinks through goodlinks_client.py (GOODLINKS_TOKEN and the
optional GOODLINKS_API base URL come from there).
"""

from __future__ import annotations

import argparse
import asyncio
import html
import sys
from datetime import datetime, timezone
from typing import Any

import goodlinks_client as gl


async def fetch_links(limit: int | None) -> list[dict[str, Any]]:
    """Page through /lists/all, stopping early once `limit` links are in hand."""
    links: list[dict[str, Any]] = []
    offset = 0
    while True:
        page_size = min(gl.API_PAGE_MAX, limit - len(links)) if limit else gl.API_PAGE_MAX
        page, has_more = await gl.fetch_links("all", limit=page_size, offset=offset)
        links.extend(page)
        if not has_more or not page or (limit and len(links) >= limit):
            return links[:limit] if limit else links
        offset += len(page)


def to_unix(value: Any) -> int | None:
    """Accept an ISO-8601 string or a number of seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def to_line(link: dict[str, Any], status_tags: bool) -> str | None:
    url = link.get("url")
    if not url:
        return None
    title = link.get("title") or url
    tags = [t for t in (link.get("tags") or []) if t]
    if status_tags:
        if link.get("readAt"):
            tags.append("goodlinks-read")
        if link.get("starred"):
            tags.append("goodlinks-starred")

    attrs = [f'HREF="{html.escape(url, quote=True)}"']
    added = to_unix(link.get("added_at") or link.get("addedAt"))
    if added is not None:
        attrs.append(f'ADD_DATE="{added}"')
    if tags:
        attrs.append(f'TAGS="{html.escape(",".join(tags), quote=True)}"')
    return f"    <DT><A {' '.join(attrs)}>{html.escape(title)}</A>"


def build(links: list[dict[str, Any]], status_tags: bool) -> str:
    lines = [ln for ln in (to_line(l, status_tags) for l in links) if ln]
    return "\n".join(
        [
            "<!DOCTYPE NETSCAPE-Bookmark-file-1>",
            '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
            "<TITLE>Bookmarks</TITLE>",
            "<H1>Bookmarks</H1>",
            "<DL><p>",
            *lines,
            "</DL><p>",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-o", "--output", default="goodlinks.html")
    parser.add_argument("--limit", type=int, help="only export the newest N links (for a test)")
    parser.add_argument(
        "--no-status-tags",
        action="store_true",
        help="do not add goodlinks-read / goodlinks-starred tags",
    )
    args = parser.parse_args()

    try:
        links = asyncio.run(fetch_links(args.limit))
    except gl.GoodLinksError as exc:
        sys.exit(str(exc))

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(build(links, status_tags=not args.no_status_tags))
    print(f"Wrote {len(links)} links to {args.output}")


if __name__ == "__main__":
    main()
