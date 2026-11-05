const CACHE = 'sweep-shell-v1';
const SHELL = [
  '/',
  '/join',
  '/live',
  '/compare',
  '/admin',
  '/static/css/app.css',
  '/static/js/common.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/manifest.webmanifest'
];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  // Never cache API, events, or results
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/api')) {
    return; // network only
  }
  // Player pages: network-first, don't put dynamic token pages in precache
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const net = fetch(e.request).then((res) => {
        // Only cache static shell assets
        if (res.ok && (url.pathname.startsWith('/static/') || SHELL.includes(url.pathname))) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      }).catch(() => cached);
      return cached || net;
    })
  );
});
