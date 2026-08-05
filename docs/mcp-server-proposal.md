# GoodLinks MCP server — capability review and proposed tool set

Status: **proposal**, nothing implemented yet.
Target protocol: MCP revision **2026-07-28** ("MCP2").

---

## 1. Sourcing caveat (read this first)

`goodlinks.app` is not reachable from the environment this document was written
in — the egress proxy denies the host — so the API details below were
reconstructed from search-engine extraction of <https://goodlinks.app/api/>
plus the working code in `goodlinks_server.py`. Everything marked
**(unverified)** should be checked against the live docs and a real
GoodLinks 3.2+ instance before it is turned into code. The tool design itself
does not hinge on the uncertain parts; the gaps are called out in §3.

---

## 2. What the GoodLinks API can do

Local HTTP server built into GoodLinks 3.2+, `http://localhost:9428/api/v1`,
enabled in Settings → API. `Authorization: Bearer <token>` on every request,
`Content-Type: application/json` on anything with a body. Localhost only,
by design — it is meant for trusted apps on the same machine.

### Reads

| Endpoint | What it gives you |
| --- | --- |
| `GET /lists/{list}` | Links from a named list. `list` ∈ `all`, `unread`, `starred`, `read`, `tagged`, `untagged` (and `highlighted`, **unverified**). Sorted by date added, newest first. |
| `GET /links?url=…` | The single link matching a URL — the "do I already have this?" lookup. |
| `GET /links` (no `url`) | Search/filter across the library, returning a list. |
| `GET /links/{id}` | One link by id, all metadata fields. `404` if absent. |
| `GET /tags` | Every tag with at least one link, as an array of strings. Hierarchical tags come back as full paths (`technology/programming`). |
| `GET /highlights` | Highlights, filterable and sortable. |

Filter parameters observed on the list/search endpoints:

- `search` — full-text over title, summary, **content**, URL, and author
- `tag` — repeatable; OR semantics (a link matching *any* listed tag is returned); ignored when `list=untagged`
- `starred`, `read`, `tagged`, `highlighted` — tri-state booleans (omit = no filter)
- `includeRead` — only meaningful for the `starred`, `untagged`, `highlighted` lists; defaults `false`
- `wordCountMin`, `wordCountMax`
- `addedAfter`, `addedBefore` — ISO-8601
- `limit` 1–1000, default **20**; `offset`, default 0

Responses carry `data` plus `hasMore` for pagination. `goodlinks_server.py`
already pages `/lists/all` at `limit=1000` until `hasMore` is false, which
confirms the shape.

### Link object

`id`, `url`, `title`, `summary`, `author`, `tags[]`, `wordCount` (nullable),
`starred`, `highlighted`, `addedAt`, `modifiedAt`, `readAt`.

### Highlight object

`id`, `linkID`, `content`, `markdownContent`, `note`, `createdAt`.
Filterable by `linkID`, `content`, `note`, `createdAfter`, `createdBefore`.

### Writes

| Endpoint | Semantics |
| --- | --- |
| `POST /links` | **Upsert.** `url` required (≤2000 chars). Optional `title` (≤200), `summary` (≤400), `tags[]`, `read`, `starred`. Missing metadata is fetched by GoodLinks itself. A URL already in the library is *updated*, not rejected. |
| `PATCH /links/{id}` | `title`, `summary`, `read`, `starred`, and either `tags` (full replace; `[]` clears all) **or** `addedTags`/`removedTags`. If `tags` is present the add/remove fields are **silently ignored**. Tags ≤100 chars, non-empty. |
| `DELETE /links?id=…` | Deletes; `id` is repeatable for batch deletion. |

Errors: `400` with detail on missing/invalid fields, `401` on a bad token,
`404` for unknown ids.

### The shape of it

Read-heavy, with a small and slightly sharp write surface. Three things drive
the tool design:

1. **Search is genuinely good.** It covers article body text, not just
   metadata. An agent can answer "what have I saved about X" in one call.
2. **`wordCount` + `readAt` + `addedAt` make time-budgeted triage possible** —
   "find me 20 minutes of unread reading" is a real query.
3. **Two write footguns**: `POST /links` silently overwrites an existing link,
   and `PATCH` silently drops `addedTags`/`removedTags` when `tags` is present.
   The MCP layer should make both impossible to hit by accident.

---

## 3. Gaps to verify before implementing

- **Highlight writes.** Only `GET /highlights` is confirmed. Whether
  `POST`/`PATCH`/`DELETE` exist is **unverified** — the docs say the API can
  "read and modify your links, tags, and highlights", which suggests writes
  exist, but no endpoint was confirmed.
- **Tag management.** No confirmed rename/delete/merge endpoint. `GET /tags`
  returning bare strings (no per-tag counts) is also **unverified**.
- **Article body retrieval.** Content is *searchable*, but no endpoint was
  confirmed that *returns* the extracted article text. If one exists it changes
  the calculus significantly — see the optional `read_article` tool in §6.
- **Sorting.** Only "newest added first" is confirmed. If a `sort`/`order`
  parameter exists, `search_links` should expose it instead of faking order
  client-side.
- **`highlighted` as a list name**, and whether `GET /links` and
  `GET /lists/all` differ in any way beyond the path.

---

## 4. Design principles

**Tools model workflows, not endpoints.** A 1:1 wrapper would be nine thin
tools that force the agent into multi-call loops for anything real. The set
below is eight tools, two of which are composites that collapse a common
loop into one call.

**Compact by default.** The full link object is ~11 fields; 100 of them is a
lot of context for a list the agent will mostly skim. List results return a
compact projection (`id`, `title`, `url`, `tags`, `wordCount`, `readAt`,
`starred`) and callers opt into `detail: "full"` when they need summaries.

**Every list result is honest about truncation.** `hasMore` and `nextOffset`
always come back, so the agent never silently reasons over a partial library.

**Errors are actionable.** Connection refused → "GoodLinks isn't running or
the API is disabled in Settings → API". `401` → "check `GOODLINKS_TOKEN`".
These are the two failures that will actually happen, and they are both
fixable by the user in ten seconds if the message says so.

**Destructive operations are explicit and confirmed** — see §5.

---

## 5. What MCP2 changes here

The 2026-07-28 revision is a good fit for this server, and two features are
worth designing *toward* rather than retrofitting:

**Stateless-first.** No `initialize` handshake, no `Mcp-Session-Id`. This
server has no per-session state to lose — the token comes from the environment
on every call — so it is stateless for free. Any caching (the existing viewer
uses a 60-second TTL) must stay a per-process optimisation, never something a
subsequent call depends on.

**Cacheable `tools/list`.** The tool list is completely static. Advertise a
long `ttlMs` (an hour) with a broad `cacheScope` so clients stop re-listing.

**Multi Round-Trip Requests.** Instead of a bolted-on `confirm: true`
parameter, destructive and ambiguous operations return
`resultType: "input_required"` and let the client collect a decision:

- `delete_links` on more than a couple of ids → confirm, listing the titles
  about to be deleted.
- `save_link` when the URL is already in the library → ask whether to update
  the existing entry, merge tags, or leave it alone. This turns the `POST`
  upsert footgun into a visible choice.

**Deprecations to avoid.** Roots, sampling, and logging are deprecated. Diagnostics
go to stderr. Nothing in this design needs sampling.

**Structured output everywhere.** Each tool declares an `outputSchema` and
returns `structuredContent` alongside one short human-readable text block —
not a JSON dump reformatted as prose.

**Transport.** stdio, or HTTP bound to `127.0.0.1`. The GoodLinks API is
localhost-only and the token is a bearer credential to the user's entire
reading history; this server should not be the thing that puts it on a
network interface.

---

## 6. Proposed tools

Eight core tools. Names are verb-first and unambiguous, because the agent
picks by name before it reads the description.

### 6.1 `search_links` — the workhorse

Covers `GET /lists/{list}` and `GET /links` search in one surface. Most
sessions will use only this tool.

```jsonc
{
  "query":        "string?",     // full-text: title, summary, body, URL, author
  "list":         "all|unread|starred|read|tagged|untagged",  // default "all"
  "tags":         "string[]?",   // OR semantics, matching the API
  "starred":      "boolean?",
  "read":         "boolean?",
  "highlighted":  "boolean?",
  "addedAfter":   "string?",     // ISO-8601 date or datetime
  "addedBefore":  "string?",
  "minWords":     "integer?",
  "maxWords":     "integer?",
  "detail":       "compact|full", // default "compact"
  "limit":        "integer?",     // default 25, max 100
  "offset":       "integer?"
}
```

Returns `{ links[], hasMore, nextOffset, appliedFilters }`.
`readOnlyHint: true`.

Notes:
- `limit` is capped at 100 even though the API allows 1000. An agent asking
  for 1000 links is about to waste a context window; if it genuinely needs the
  whole library it should be using `summarize_queue`.
- `appliedFilters` echoes what was actually sent, which makes the inevitable
  "why didn't my tag filter work" debugging one call instead of three.
- Tag semantics are OR. The description must say so explicitly, or the agent
  will assume AND and quietly mis-answer.

### 6.2 `get_link`

```jsonc
{
  "id":                "string?",   // exactly one of id / url
  "url":               "string?",
  "includeHighlights": "boolean?"   // default false
}
```

Wraps `GET /links/{id}` and `GET /links?url=…`. `includeHighlights` folds in
the `GET /highlights?linkID=…` call that otherwise always follows.
`readOnlyHint: true`.

Doubles as the "is this already saved?" check, which is what makes
`save_link` safe to call.

### 6.3 `save_link`

```jsonc
{
  "url":       "string",       // required
  "title":     "string?",      // ≤200, omit to let GoodLinks fetch it
  "summary":   "string?",      // ≤400
  "tags":      "string[]?",
  "read":      "boolean?",
  "starred":   "boolean?",
  "onExisting": "ask|update|merge_tags|skip"  // default "ask"
}
```

`POST /links` is an upsert, so the tool checks for an existing link first and
branches on `onExisting`. The default `ask` returns
`resultType: "input_required"` rather than silently overwriting a link the
user curated months ago. Returns `{ link, outcome: "created"|"updated"|"skipped" }`.
`idempotentHint: true`.

Client-side validation of the length limits, so a 250-character title fails
with a useful message instead of a bare `400`.

### 6.4 `update_link`

```jsonc
{
  "id":         "string",      // required
  "title":      "string?",
  "summary":    "string?",
  "read":       "boolean?",
  "starred":    "boolean?",
  "tags":       "string[]?",   // full replace; [] clears
  "addTags":    "string[]?",
  "removeTags": "string[]?"
}
```

Maps to `PATCH /links/{id}`. The one behavioural addition: passing `tags`
together with `addTags`/`removeTags` is **rejected with an explanatory error**
rather than passed through to be silently ignored. Returns the updated link.
`idempotentHint: true`.

### 6.5 `delete_links`

```jsonc
{ "ids": "string[]" }   // non-empty
```

`DELETE /links?id=…&id=…`. `destructiveHint: true`, `readOnlyHint: false`.
Resolves each id to a title first, then — via MRT — presents the list and
requires confirmation before deleting more than one. Refuses an empty array.
Returns `{ deleted: [...], notFound: [...] }`.

### 6.6 `list_tags`

```jsonc
{ "prefix": "string?" }   // e.g. "tech/" for hierarchical children
```

`GET /tags`. `readOnlyHint: true`. The `prefix` filter is applied server-side
by this MCP server, since the tag list is small and the API takes no filter.

Worth having as its own tool: it is how the agent discovers the user's actual
vocabulary before tagging anything, which is the difference between
`machine-learning` and a fourth near-duplicate `ML` tag.

### 6.7 `get_highlights`

```jsonc
{
  "linkId":        "string?",
  "query":         "string?",   // matches highlight content
  "noteQuery":     "string?",   // matches attached notes
  "createdAfter":  "string?",
  "createdBefore": "string?",
  "format":        "structured|markdown",  // default "structured"
  "limit":         "integer?",  // default 50
  "offset":        "integer?"
}
```

`GET /highlights`. `format: "markdown"` returns the highlights of a link as a
ready-to-paste block using `markdownContent`, which is the common
export-to-notes workflow. `readOnlyHint: true`.

### 6.8 `summarize_queue` — the composite

No single endpoint backs this; it is several paged calls the agent would
otherwise make itself, and it is the tool that makes the server useful for
"what should I read?" rather than just "fetch me rows".

```jsonc
{
  "minutesAvailable": "integer?",   // e.g. 20
  "tags":             "string[]?",
  "sample":           "integer?"    // suggested items to return, default 5
}
```

Returns:

```jsonc
{
  "counts":       { "total": 0, "unread": 0, "starred": 0, "untagged": 0 },
  "unreadWords":  0,
  "unreadHours":  0.0,            // at ~230 wpm
  "oldestUnread": { "title": "", "addedAt": "" },
  "topTags":      [ { "tag": "", "count": 0 } ],
  "staleCount":   0,              // unread, added > 6 months ago
  "suggestions":  [ /* compact links fitting minutesAvailable */ ]
}
```

`readOnlyHint: true`. When `minutesAvailable` is given, `suggestions` picks
unread links whose `wordCount` fits the budget, preferring starred and older
items. Reading speed is a stated assumption, not a fact — it belongs in the
tool description so the agent can caveat it.

### Optional, pending verification

- **`bulk_update_links`** — apply the same `read`/`starred`/`addTags`/
  `removeTags` change across many ids (a loop of `PATCH`es). Real triage
  ("archive everything tagged `news` older than a year") is 40 calls without
  it. Worth adding once the core set is proven.
- **`read_article`** — return the extracted article body for a link. Only
  possible if such an endpoint exists (§3). This would be the single highest-value
  addition, since it lets an agent summarise a saved article without
  re-fetching it from the web.
- **`rename_tag` / `merge_tags`** — blocked on tag-management endpoints
  existing. Emulating a rename via per-link `PATCH` is possible but a bad idea
  as a first implementation: it is O(links) write operations with no
  transaction.

### Deliberately not included

- One tool per list name (`get_unread`, `get_starred`, …) — that is a `list`
  parameter, not seven tools.
- A raw `goodlinks_request(method, path, body)` escape hatch. It defeats
  input validation, gives the model an unbounded destructive surface, and
  makes the confirmation flows in `delete_links`/`save_link` bypassable.
- Resources for individual links. Possible (`goodlinks://link/{id}`), but
  `get_link` already covers it and clients vary in resource support.

---

## 7. Implementation notes

The HTTP client already exists in `goodlinks_server.py` — pagination, the
`401` handling, and the "is GoodLinks even running" error path. Pulling
`fetch_all_links` and the auth setup into a shared `goodlinks_client.py` would
let the viewer and the MCP server share one client and one set of error
messages, rather than drifting apart.

Suggested layout:

```
goodlinks_client.py     # shared async client: auth, paging, error translation
goodlinks_server.py     # existing FastAPI viewer, imports the client
goodlinks_mcp.py        # MCP2 server, imports the client
```

Config stays environment-driven and identical to today: `GOODLINKS_TOKEN`,
`GOODLINKS_API`.

Phasing:

1. Shared client + `search_links`, `get_link`, `list_tags` (read-only; useful
   immediately, nothing can be damaged).
2. `save_link`, `update_link`, `get_highlights`.
3. `delete_links` with the MRT confirmation flow, then `summarize_queue`.
4. Optional tools, once §3 is resolved against a live instance.

---

## 8. Open questions

1. Should the MCP server reuse the viewer's 60-second cache? It makes repeated
   agent calls fast, but a stale read immediately after a write is confusing.
   Recommendation: cache reads, invalidate the whole cache on any write.
2. Is `summarize_queue`'s ~230 wpm assumption worth making configurable via an
   env var, or is a documented constant fine?
3. Does the user want write tools at all in the first cut, or is a read-only
   server the right place to start?
