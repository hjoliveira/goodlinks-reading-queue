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

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

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


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Reading queue</title>
<style>
  :root {
    --bg: #f7f7f5;
    --ink: #1a1c20;
    --muted: #70757e;
    --line: #e4e4e0;
    --accent: #2352a0;          /* azulejo cobalt */
    --accent-soft: #2352a014;
    --serif: Charter, "Iowan Old Style", Georgia, serif;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #17181b;
      --ink: #ececec;
      --muted: #8f939c;
      --line: #2b2d32;
      --accent: #7da7e8;
      --accent-soft: #7da7e81f;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: var(--sans);
    line-height: 1.5;
  }
  .wrap { max-width: 680px; margin: 0 auto; padding: 3rem 1.25rem 5rem; }

  header { margin-bottom: 2rem; }
  h1 {
    font-family: var(--serif);
    font-size: 1.9rem;
    font-weight: 700;
    margin: 0;
    letter-spacing: -0.01em;
  }
  .queue-line {
    color: var(--muted);
    font-size: 0.92rem;
    margin-top: 0.3rem;
  }
  .queue-line strong { color: var(--accent); font-weight: 600; }

  .controls {
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.75rem;
  }
  input[type="search"], select {
    font: inherit;
    font-size: 0.9rem;
    color: var(--ink);
    background: transparent;
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 0.45rem 0.7rem;
  }
  input[type="search"] { flex: 1 1 200px; }
  input[type="search"]:focus, select:focus {
    outline: none;
    border-color: var(--accent);
  }

  .filters { display: flex; gap: 0.35rem; margin-bottom: 1.5rem; }
  .filters button {
    font: inherit;
    font-size: 0.82rem;
    padding: 0.25rem 0.7rem;
    border: 1px solid var(--line);
    border-radius: 999px;
    background: transparent;
    color: var(--muted);
    cursor: pointer;
  }
  .filters button.active {
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent);
  }

  .item {
    padding: 1rem 0;
    border-bottom: 1px solid var(--line);
  }
  .item a.title {
    font-family: var(--serif);
    font-size: 1.12rem;
    font-weight: 600;
    color: var(--ink);
    text-decoration: none;
  }
  .item a.title:hover { color: var(--accent); }
  .item.read a.title { color: var(--muted); font-weight: 400; }
  .meta {
    font-size: 0.8rem;
    color: var(--muted);
    margin-top: 0.2rem;
    display: flex;
    gap: 0.45rem;
    flex-wrap: wrap;
    align-items: baseline;
  }
  .star { color: var(--accent); }
  .tag {
    color: var(--accent);
    cursor: pointer;
  }
  .tag:hover { text-decoration: underline; }
  .summary {
    font-size: 0.88rem;
    color: var(--muted);
    margin-top: 0.3rem;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .status { text-align: center; color: var(--muted); padding: 3rem 0; }
  .status.error { color: #b4432e; }
  @media (prefers-reduced-motion: no-preference) {
    .item { animation: rise 0.25s ease both; }
    @keyframes rise { from { opacity: 0; transform: translateY(4px); } }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Reading queue</h1>
    <div class="queue-line" id="queue-line">Loading your library…</div>
  </header>

  <div class="controls" hidden id="controls">
    <input type="search" id="search" placeholder="Search title, URL, author…">
    <select id="tag-filter"><option value="">All tags</option></select>
  </div>
  <div class="filters" hidden id="filters">
    <button data-f="unread" class="active">Unread</button>
    <button data-f="all">All</button>
    <button data-f="starred">Starred</button>
    <button data-f="read">Read</button>
  </div>

  <div id="list"><div class="status">Loading…</div></div>
</div>

<script>
(function () {
  const $ = (id) => document.getElementById(id);
  const WPM = 230;
  let links = [];
  let mode = "unread";
  let activeTag = "";

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const host = (u) => { try { return new URL(u).hostname.replace(/^www\\./, ""); } catch { return ""; } };

  const readTime = (wc) => {
    if (!wc) return "";
    const min = Math.max(1, Math.round(wc / WPM));
    return min + " min";
  };

  const fmtDate = (iso) => {
    if (!iso) return "";
    return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  };

  const fmtHours = (words) => {
    const min = Math.round(words / WPM);
    if (min < 60) return "~" + min + " min";
    return "~" + (min / 60).toFixed(min < 600 ? 1 : 0).replace(/\\.0$/, "") + " h";
  };

  function queueLine() {
    const unread = links.filter((l) => !l.readAt);
    const words = unread.reduce((sum, l) => sum + (l.wordCount || 0), 0);
    const parts = ["<strong>" + unread.length + " unread</strong>"];
    if (words) parts.push(fmtHours(words) + " of reading");
    parts.push(links.length + " links saved");
    $("queue-line").innerHTML = parts.join(" · ");
  }

  function filtered() {
    const q = $("search").value.trim().toLowerCase();
    return links.filter((l) => {
      if (mode === "unread" && l.readAt) return false;
      if (mode === "read" && !l.readAt) return false;
      if (mode === "starred" && !l.starred) return false;
      if (activeTag && !(l.tags || []).includes(activeTag)) return false;
      if (q) {
        const hay = [l.title, l.url, l.summary, l.author, (l.tags || []).join(" ")]
          .join(" ").toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  function render() {
    const rows = filtered();
    if (!rows.length) {
      $("list").innerHTML = '<div class="status">Nothing here. Adjust the filters or save something worth reading.</div>';
      return;
    }
    $("list").innerHTML = rows.map((l) => `
      <div class="item${l.readAt ? " read" : ""}">
        <a class="title" href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.title || l.url)}</a>
        <div class="meta">
          ${l.starred ? '<span class="star" title="Starred">&#9733;</span>' : ""}
          <span>${esc(host(l.url))}</span>
          ${l.wordCount ? "<span>&middot;</span><span>" + readTime(l.wordCount) + "</span>" : ""}
          <span>&middot;</span><span>${fmtDate(l.addedAt)}</span>
          ${(l.tags || []).map((t) =>
            '<span class="tag" data-tag="' + esc(t) + '">#' + esc(t) + "</span>").join(" ")}
        </div>
        ${l.summary ? '<div class="summary">' + esc(l.summary) + "</div>" : ""}
      </div>`).join("");
  }

  function populateTags() {
    const tags = [...new Set(links.flatMap((l) => l.tags || []))].sort((a, b) => a.localeCompare(b));
    $("tag-filter").innerHTML = '<option value="">All tags</option>' +
      tags.map((t) => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  }

  $("search").addEventListener("input", render);
  $("tag-filter").addEventListener("input", (e) => { activeTag = e.target.value; render(); });
  $("filters").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    mode = b.dataset.f;
    document.querySelectorAll(".filters button").forEach((x) => x.classList.toggle("active", x === b));
    render();
  });
  $("list").addEventListener("click", (e) => {
    const t = e.target.closest(".tag");
    if (!t) return;
    activeTag = activeTag === t.dataset.tag ? "" : t.dataset.tag;
    $("tag-filter").value = activeTag;
    render();
  });

  fetch("/api/links")
    .then(async (r) => {
      if (!r.ok) throw new Error((await r.json()).detail || "Request failed (" + r.status + ")");
      return r.json();
    })
    .then((data) => {
      links = data.links;
      $("controls").hidden = false;
      $("filters").hidden = false;
      queueLine();
      populateTags();
      render();
    })
    .catch((err) => {
      $("queue-line").textContent = "Library unavailable";
      $("list").innerHTML = '<div class="status error">' + esc(err.message) + "</div>";
    });
})();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set GOODLINKS_TOKEN (Settings -> API in GoodLinks).")
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8300")),
    )
