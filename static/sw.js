// voice-slides — offline service worker.
//
// The cache name IS the version, and the version arrives from one place: the
// server substitutes __VERSION__ out of the repo's VERSION file on the way to
// the browser. Bumping that file changes these bytes, which is what makes the
// browser install a new worker at all.
//
// Cache-first for the precached shell; a navigation falls back to its own
// cached page (then the presenter) so both windows open with the network gone.
// That is not a nicety here — the popout's whole claim is that it presents off
// a dead network.

const VERSION = 'voice-slides-v__VERSION__';
const PRECACHE = [
  './',
  './index.html',
  './present.html',
  './app.css',
  './render.js',
  './sw-register.js',
  './vendor/mermaid.min.js',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-512-maskable.png',
  './icons/apple-touch-icon.png',
];

// `cache: 'reload'` makes each precache fetch skip the HTTP cache, so a stale
// entry left by an earlier max-age can never be copied into a new version's
// cache. addAll stays atomic: one failed fetch aborts install, which is why
// test_app.py checks every path above exists on disk.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll(
      PRECACHE.map((u) => new Request(u, { cache: 'reload' }))
    ))
  );
});

// A new worker waits until the page asks for it, so a deploy never reloads the
// app mid-talk. sw-register.js sends this when the user taps Reload.
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if (new URL(req.url).pathname === '/ws') return;   // the socket is not cacheable

  // Each navigation refreshes the cache under its OWN key. Keying every
  // navigation as './index.html' would store the popout as the app shell, and
  // the next offline launch would open the audience view instead of the desk.
  if (req.mode === 'navigate') {
    const path = new URL(req.url).pathname;
    const key = (path === '/' || path.endsWith('/index.html')) ? './index.html' : req;
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(key, copy));
          return res;
        })
        .catch(() => caches.match(req, { ignoreSearch: true })
          .then((hit) => hit || caches.match('./index.html', { ignoreSearch: true }))
          .then((hit) => hit || caches.match('./')))
    );
    return;
  }

  // ignoreSearch is a matched pair with the ?v= stamps on the asset links:
  // without it a stamped URL misses the precached bare entry offline.
  event.respondWith(
    caches.match(req, { ignoreSearch: true }).then((hit) => {
      if (hit) return hit;
      return fetch(req).then((res) => {
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy));
        }
        return res;
      });
    })
  );
});
