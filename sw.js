// Matchday Edge service worker: lets the app install and open without internet.
// Change VERSION whenever index.html changes, so phones pick up the new page.
const VERSION = "me-v2";
const SHELL = ["./", "index.html", "manifest.json", "icon-192.png", "icon-512.png", "apple-touch-icon.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;  // team crests etc. load normally
  // Always try the network first so predictions are fresh; fall back to the saved copy offline.
  const key = url.origin + url.pathname;
  e.respondWith(
    fetch(e.request).then(res => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(VERSION).then(c => c.put(key, copy));
      }
      return res;
    }).catch(() => caches.match(key).then(hit => hit || caches.match("index.html")))
  );
});
