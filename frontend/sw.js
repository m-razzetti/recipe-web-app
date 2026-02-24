const CACHE_VERSION = "v3.0.0";
const STATIC_CACHE = `recipes-static-${CACHE_VERSION}`;
const API_CACHE = `recipes-api-${CACHE_VERSION}`;
const PHOTO_CACHE = `recipes-photos-${CACHE_VERSION}`;

const STATIC_ASSETS = [
  "/",
  "/index.html",
  "/css/base.css",
  "/css/layout.css",
  "/css/components.css",
  "/css/modal.css",
  "/css/print.css",
  "/css/mobile.css",
  "/manifest.webmanifest",
  "/razz.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(STATIC_ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => ![STATIC_CACHE, API_CACHE, PHOTO_CACHE].includes(key))
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const cached = await cache.match(request);
  const fetchPromise = fetch(request)
    .then((response) => {
      if (response && response.ok) {
        cache.put(request, response.clone());
      }
      return response;
    })
    .catch(() => null);

  if (cached) return cached;
  const network = await fetchPromise;
  if (network) return network;
  return new Response("Offline", { status: 503 });
}

async function networkFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (_) {
    const cached = await cache.match(request);
    if (cached) return cached;
    return new Response("Offline", { status: 503 });
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match("/index.html"))
    );
    return;
  }

  if (url.pathname.startsWith("/api/photos/")) {
    event.respondWith(staleWhileRevalidate(request, PHOTO_CACHE));
    return;
  }

  if (url.pathname.startsWith("/api/recipes")) {
    event.respondWith(networkFirst(request, API_CACHE));
    return;
  }

  if (STATIC_ASSETS.includes(url.pathname)) {
    event.respondWith(staleWhileRevalidate(request, STATIC_CACHE));
    return;
  }
});
