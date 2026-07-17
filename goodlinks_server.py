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

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

PAGE_FILE = Path(__file__).resolve().parent / "index.html"

GOODLINKS_API = os.environ.get("GOODLINKS_API", "http://localhost:9428/api/v1")
TOKEN = os.environ.get("GOODLINKS_TOKEN", "")
PAGE_SIZE = 1000  # GoodLinks API maximum per page
CACHE_TTL = 60    # seconds

app = FastAPI(title="GoodLinks Viewer")

_cache: dict = {"at": 0.0, "links": None}


async def fetch_all_links() -> list[dict]:
    """Page through /lists/all and return every link in the library."""
    links: list[dict] = []
    offset = 0
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            resp = await client.get(
                f"{GOODLINKS_API}/lists/all",
                params={"limit": PAGE_SIZE, "offset": offset},
                headers=headers,
            )
            if resp.status_code == 401:
                raise HTTPException(502, "GoodLinks rejected the token (401). Check GOODLINKS_TOKEN.")
            resp.raise_for_status()
            page = resp.json()
            links.extend(page["data"])
            if not page.get("hasMore"):
                return links
            offset += PAGE_SIZE


@app.get("/api/links")
async def api_links(refresh: bool = False) -> JSONResponse:
    now = time.monotonic()
    if refresh or _cache["links"] is None or now - _cache["at"] > CACHE_TTL:
        try:
            _cache["links"] = await fetch_all_links()
            _cache["at"] = time.monotonic()
        except httpx.ConnectError:
            raise HTTPException(
                502,
                "Could not reach the GoodLinks API. Is GoodLinks running and the API "
                f"enabled? (tried {GOODLINKS_API})",
            )
    return JSONResponse({"links": _cache["links"]})


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE_FILE.read_text(encoding="utf-8")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set GOODLINKS_TOKEN (Settings -> API in GoodLinks).")
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8300")),
    )
