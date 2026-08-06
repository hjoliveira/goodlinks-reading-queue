# GoodLinks reading queue

A small local web front-end for the [GoodLinks](https://goodlinks.app) API: browse, search, and filter your reading queue from any browser. The API token stays server-side; the browser only talks to this app.

## Requirements

- macOS with GoodLinks 3.2+ running, and the API enabled (GoodLinks → Settings → API)
- [uv](https://docs.astral.sh/uv/) — dependencies live in `pyproject.toml` and are pinned by `uv.lock`; `uv run` provisions the environment on first use, so there is nothing to install by hand
- `index.html`, `sw.js`, and `goodlinks_client.py` must sit next to `goodlinks_server.py` (the first two are served from disk; the third is the shared GoodLinks API client)

## Setup

Both servers take their configuration from a `.env` file, which `uv` loads via
`--env-file`. Create it once:

```sh
cp .env.example .env
```

then put your token in it — it's shown in GoodLinks under Settings → API:

```sh
GOODLINKS_TOKEN=your-api-token
```

`.env` is gitignored, and since it holds a credential to your whole reading
history, `chmod 600 .env` is worth the two seconds.

## Start

```sh
uv run --env-file .env goodlinks_server.py
```

Then open <http://127.0.0.1:8300>.

## Configuration

Everything below goes in `.env` (see `.env.example`). All of it is optional —
only `GOODLINKS_TOKEN` is required.

| Variable         | Default                          | Purpose                                              |
| ---------------- | -------------------------------- | ---------------------------------------------------- |
| `GOODLINKS_API`  | `http://localhost:9428/api/v1`   | Base URL of the GoodLinks API                        |
| `PORT`           | `8300`                           | Port for this server                                 |
| `HOST`           | `127.0.0.1`                      | Bind address (`0.0.0.0` to expose on your Tailnet)   |

Variables already set in your shell win over the file, so a one-off override
still works without editing it:

```sh
PORT=9000 uv run --env-file .env goodlinks_server.py
```

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
uv run --env-file .env goodlinks_mcp.py
```

Or register it with a client — for Claude Code:

```sh
claude mcp add goodlinks -- uv run --project /full/path/to/repo --env-file /full/path/to/.env /full/path/to/goodlinks_mcp.py
```

For Claude Desktop, add this to
`~/Library/Application Support/Claude/claude_desktop_config.json` (create the
file if it isn't there), then quit and reopen Claude Desktop — closing the
window is not enough:

```json
{
  "mcpServers": {
    "goodlinks": {
      "command": "/Users/you/.local/bin/uv",
      "args": [
        "run",
        "--project", "/Users/you/goodlinks-reading-queue",
        "--env-file", "/Users/you/goodlinks-reading-queue/.env",
        "/Users/you/goodlinks-reading-queue/goodlinks_mcp.py"
      ]
    }
  }
}
```

Two things about that config are load-bearing:

- **`command` is the absolute path to `uv`**, not `"uv"`. Claude Desktop
  launches servers from the macOS app environment rather than your shell, so
  it does not see the `PATH` your terminal has, and `~/.local/bin/uv` is
  invisible to it. Run `which uv` and paste the result (Homebrew installs sit
  at `/opt/homebrew/bin/uv`). A server that never appears, with an `ENOENT` in
  `~/Library/Logs/Claude/mcp-server-goodlinks.log`, is this.
- **Every path is absolute, and `--project` is required.** A client launches
  the server from whatever working directory it happens to have. `uv`
  discovers `pyproject.toml` from the *working directory*, not from the script
  path, so without `--project` it finds no project, installs no dependencies,
  and the server dies with `ModuleNotFoundError: No module named 'mcp'`. The
  same reasoning applies to `--env-file`. The script's own imports are fine
  either way — Python resolves `goodlinks_client` relative to the script.

You can inline the token as an `"env": {"GOODLINKS_TOKEN": "..."}` block
instead of using `--env-file`. It works, but it puts the credential in a file
that is easier to share by accident than `.env` is.

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

## Tests

```sh
uv run test_goodlinks.py
```

No GoodLinks instance, network, or `.env` is needed — every HTTP call is served
by an `httpx.MockTransport`, so the suite is hermetic and runs in well under a
second. It covers the shared client's error translation and query-parameter
handling, the MCP server's projection, pagination, and tool schemas, and the
viewer's paging, caching, and error mapping.

## Offline cache

The browser caches the page and the last fetched link list client-side (a service worker plus Cache Storage). If the server or GoodLinks is unreachable — laptop asleep, app quit, API disabled — the page still loads with the last known list and shows an "Offline — cached list from …" notice. The cache is per-browser and is filled on the first successful visit, so a browser that has never loaded the page while the server was up has nothing to fall back on.

## License

[MIT](LICENSE). GoodLinks itself is a separate commercial app and is not covered by this licence.
