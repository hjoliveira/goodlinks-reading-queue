# GoodLinks reading queue

A small local web front-end for the [GoodLinks](https://goodlinks.app) API: browse, search, and filter your reading queue from any browser. The API token stays server-side; the browser only talks to this app.

## Requirements

- macOS with GoodLinks 3.2+ running, and the API enabled (GoodLinks → Settings → API)
- [uv](https://docs.astral.sh/uv/) — the script declares its own dependencies (FastAPI, uvicorn, httpx), so no manual install is needed
- `index.html` and `sw.js` must sit next to `goodlinks_server.py` (they're served from disk)

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

## Offline cache

The browser caches the page and the last fetched link list client-side (a service worker plus Cache Storage). If the server or GoodLinks is unreachable — laptop asleep, app quit, API disabled — the page still loads with the last known list and shows an "Offline — cached list from …" notice. The cache is per-browser and is filled on the first successful visit, so a browser that has never loaded the page while the server was up has nothing to fall back on.
