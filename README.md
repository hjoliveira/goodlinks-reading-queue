# GoodLinks reading queue

A small local web front-end for the [GoodLinks](https://goodlinks.app) API: browse, search, and filter your reading queue from any browser. The API token stays server-side; the browser only talks to this app.

## Requirements

- macOS with GoodLinks 3.2+ running, and the API enabled (GoodLinks → Settings → API)
- [uv](https://docs.astral.sh/uv/) — the script declares its own dependencies (FastAPI, uvicorn, httpx), so no manual install is needed
- `index.html` must sit next to `goodlinks_server.py` (it's served from disk)

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

Links are cached for 60 seconds; hit `/api/links?refresh=true` to force a refetch.
