# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi>=0.115",
#     "uvicorn>=0.30",
#     "httpx>=0.27",
# ]
# ///
"""Local web front-end for the GoodLinks API.

Runs on the same Mac as GoodLinks (3.2+, API enabled in Settings -> API).
The API token stays server-side; the browser only talks to this app.

Talks to GoodLinks through goodlinks_client.py, which must sit next to this
file (as index.html and sw.js do).

Usage:
    GOODLINKS_TOKEN=your-api-token uv run goodlinks_server.py

Optional environment variables:
    GOODLINKS_API   Base URL of the GoodLinks API (default http://localhost:9428/api/v1)
    PORT            Port for this server (default 8300)
    HOST            Bind address (default 127.0.0.1; use 0.0.0.0 to expose on your Tailnet)
"""

import os
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import goodlinks_client as gl

BASE_DIR = Path(__file__).resolve().parent

CACHE_TTL = 60  # seconds

app = FastAPI(title="GoodLinks Viewer")

_cache: dict = {"at": 0.0, "links": None}


async def fetch_all_links() -> list[dict]:
    """Page through /lists/all and return every link in the library."""
    links: list[dict] = []
    offset = 0
    while True:
        page, has_more = await gl.fetch_links("all", limit=gl.API_PAGE_MAX, offset=offset)
        links.extend(page)
        if not has_more or not page:
            return links
        offset += len(page)


@app.get("/api/links")
async def api_links(refresh: bool = False) -> JSONResponse:
    now = time.monotonic()
    if refresh or _cache["links"] is None or now - _cache["at"] > CACHE_TTL:
        try:
            _cache["links"] = await fetch_all_links()
            _cache["at"] = time.monotonic()
        except gl.GoodLinksError as exc:
            raise HTTPException(502, str(exc))
    return JSONResponse({"links": _cache["links"]})


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (BASE_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    return FileResponse(BASE_DIR / "sw.js", media_type="text/javascript")


if __name__ == "__main__":
    if not gl.TOKEN:
        raise SystemExit("Set GOODLINKS_TOKEN (Settings -> API in GoodLinks).")
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8300")),
    )
