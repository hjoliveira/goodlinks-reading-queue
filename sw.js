/* Offline fallback for the reading queue.
 *
 * Network-first for the page shell and /api/links; every good response is
 * copied into Cache Storage. When the server is unreachable (or errors),
 * the cached copy is served instead, marked with X-Stale/X-Cached-At so
 * the page can tell the list is old. */

const CACHE = "goodlinks-viewer-v1";

// /api/links and /api/links?refresh=true share one cache slot.
const cacheKey = (url) => (url.pathname === "/api/links" ? "/api/links" : url.pathname);

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.add("/")).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function networkFirst(request, key) {
  const cache = await caches.open(CACHE);
  let resp = null;
  try {
    resp = await fetch(request);
  } catch {
    // server unreachable
  }
  if (resp && resp.ok) {
    const body = await resp.clone().arrayBuffer();
    const headers = new Headers(resp.headers);
    headers.set("X-Cached-At", new Date().toISOString());
    await cache.put(key, new Response(body, { status: 200, headers }));
    return resp;
  }
  const cached = await cache.match(key);
  if (cached) {
    const headers = new Headers(cached.headers);
    headers.set("X-Stale", "true");
    return new Response(await cached.clone().arrayBuffer(), { status: 200, headers });
  }
  if (resp) return resp; // real error and nothing cached: let the page report it
  return Response.error();
}

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname === "/" || url.pathname === "/api/links") {
    e.respondWith(networkFirst(e.request, cacheKey(url)));
  }
});
