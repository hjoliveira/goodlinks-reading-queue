# GoodLinks reading queue

A small local web front-end for the [GoodLinks](https://goodlinks.app) API: browse, search, and filter your reading queue from any browser. The API token stays server-side; the browser only talks to this app.

## Requirements

- macOS with GoodLinks 3.2+ running, and the API enabled (GoodLinks → Settings → API)
- [uv](https://docs.astral.sh/uv/) — the script declares its own dependencies (FastAPI, uvicorn, httpx), so no manual install is needed
- `index.html`, `sw.js`, and `goodlinks_client.py` must sit next to `goodlinks_server.py` (the first two are served from disk; the third is the shared GoodLinks API client)

## Start

```sh
GOODLINKS_TOKEN=your-api-token uv run goodlinks_server.py
```

Then open <http://127.0.0.1:8300>.

The token is shown in GoodLinks under Settings → API.

## Configuration

Optional environment variables:

| Variable         | Default                          | Purpose                                              |
| ---------------- | -------------------------------- | ---------------------------------------------------- |
| `GOODLINKS_API`  | `http://localhost:9428/api/v1`   | Base URL of the GoodLinks API                        |
| `PORT`           | `8300`                           | Port for this server                                 |
| `HOST`           | `127.0.0.1`                      | Bind address (`0.0.0.0` to expose on your Tailnet)   |

The server caches links for 60 seconds; hit `/api/links?refresh=true` to force a refetch.

## MCP server

`goodlinks_mcp.py` exposes the same library to an MCP client (Claude Code,
Claude Desktop, or anything else that speaks MCP) as three **read-only** tools:

| Tool | What it does |
| ---- | ------------ |
| `goodlinks_search_links` | Full-text search over titles, summaries, article body text, URLs, and authors. |
| `goodlinks_list_links` | A page of links from a named list: `all`, `unread`, `starred`, `read`, `tagged`, `untagged`. |
| `goodlinks_get_article_content` | The article text GoodLinks extracted, as markdown, plaintext, or HTML. |

The server only ever issues GET requests, so nothing it does can change or
delete a link.

Run it directly:

```sh
GOODLINKS_TOKEN=your-api-token uv run goodlinks_mcp.py
```

Or register it with a client — for Claude Code:

```sh
claude mcp add goodlinks -- uv run /full/path/to/goodlinks_mcp.py
```

with `GOODLINKS_TOKEN` set in that client's environment.

Both list tools return at most 200 links per call and report `has_more` plus a
`next_offset` to page with; long articles come back in `max_chars` slices with
a `next_char_offset`. Results are compact by default — pass `detail: "full"`
for summaries, authors, and timestamps.

It speaks stdio by default. Setting `MCP_TRANSPORT=streamable-http` (with
optional `MCP_HOST`, default `127.0.0.1`, and `MCP_PORT`, default `8301`)
serves it over HTTP instead, which is the only transport on which the
2026-07-28 protocol revision — and so the server's `tools/list` cache hints —
is reachable; the stdio handshake tops out at 2025-11-25. Keep it on loopback
either way: the token grants access to your entire reading history.

## Offline cache

The browser caches the page and the last fetched link list client-side (a service worker plus Cache Storage). If the server or GoodLinks is unreachable — laptop asleep, app quit, API disabled — the page still loads with the last known list and shows an "Offline — cached list from …" notice. The cache is per-browser and is filled on the first successful visit, so a browser that has never loaded the page while the server was up has nothing to fall back on.
