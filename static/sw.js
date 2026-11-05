/* Served from /sw.js with the deploy id filled in as CACHE. */
const CACHE = "__SW_CACHE_NAME__";
const SHELL = [
  "/",
  "/join",
  "/live",
  "/compare",
  "/admin",
  "/open",
  "/open/live",
  "/recap",
  "/open/recap",
  "/coalition",
  "/results",
  "/news",
  "/seats",
  "/static/css/app.css",
  "/static/js/common.js",
  "/static/js/night.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/manifest.webmanifest"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

function isApi(url) {
  return url.pathname === "/api" || url.pathname.startsWith("/api/");
}

function wantsHtml(request) {
  if (request.mode === "navigate") return true;
  const accept = request.headers.get("accept") || "";
  return accept.indexOf("text/html") !== -1;
}

function isStaticAsset(url) {
  return url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest";
}

function freshFetch(request) {
  // Chromium rejects fetch(navigateRequest, init). A new Request from the URL
  // still bypasses the HTTP cache via cache:'reload'.
  if (request.mode === "navigate") {
    return fetch(new Request(request.url, {
      method: "GET",
      headers: { Accept: request.headers.get("Accept") || "text/html" },
      cache: "reload",
      credentials: "same-origin",
      redirect: "follow"
    }));
  }
  try {
    return fetch(request, { cache: "reload" });
  } catch (err) {
    return fetch(request);
  }
}

function store(url, response) {
  if (!response || !response.ok || response.type !== "basic") return;
  const copy = response.clone();
  caches.open(CACHE).then((cache) => cache.put(url, copy)).catch(() => {});
}

function networkFirst(request) {
  return freshFetch(request).then((response) => {
    store(request.url, response);
    return response;
  }).catch(() => caches.match(request).then((cached) => {
    if (cached) return cached;
    if (request.mode === "navigate") return caches.match(new URL(request.url).pathname);
    return undefined;
  }).then((cached) => cached || new Response("Offline", {
    status: 503,
    headers: { "Content-Type": "text/plain; charset=utf-8" }
  })));
}

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (event.request.method !== "GET") return;
  // /api, including SSE /api/events, is network-only.
  if (isApi(url)) return;
  if (wantsHtml(event.request) || isStaticAsset(url)) {
    event.respondWith(networkFirst(event.request));
  }
});
