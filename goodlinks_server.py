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
    GOODLINKS_CACHE Path of the on-disk link cache used when GoodLinks is unreachable
                    (default ~/.cache/goodlinks-viewer/links.json)
"""

import json
import os
import time
from datetime import datetime, timezone
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
CACHE_FILE = Path(
    os.environ.get("GOODLINKS_CACHE")
    or Path.home() / ".cache" / "goodlinks-viewer" / "links.json"
)

app = FastAPI(title="GoodLinks Viewer")

_cache: dict = {"at": 0.0, "links": None, "saved_at": None, "stale": False}


def _load_disk_cache() -> tuple[list[dict] | None, str | None]:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data["links"], data.get("savedAt")
    except (OSError, ValueError, KeyError):
        return None, None


def _save_disk_cache(links: list[dict], saved_at: str) -> None:
    # Best-effort: an unwritable cache dir shouldn't break serving.
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"savedAt": saved_at, "links": links}), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except OSError:
        pass


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
            links = await fetch_all_links()
        except httpx.HTTPError:
            # GoodLinks is down: fall back to the last known list, from memory
            # or (after a restart) from disk.
            if _cache["links"] is None:
                _cache["links"], _cache["saved_at"] = _load_disk_cache()
            if _cache["links"] is None:
                raise HTTPException(
                    502,
                    "Could not reach the GoodLinks API and no cached copy exists yet. "
                    f"Is GoodLinks running and the API enabled? (tried {GOODLINKS_API})",
                )
            _cache["stale"] = True
            _cache["at"] = now  # retry after TTL instead of on every request
        else:
            _cache["links"] = links
            _cache["at"] = time.monotonic()
            _cache["stale"] = False
            _cache["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _save_disk_cache(links, _cache["saved_at"])
    body: dict = {"links": _cache["links"], "stale": _cache["stale"]}
    if _cache["stale"]:
        body["cachedAt"] = _cache["saved_at"]
    return JSONResponse(body)


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
