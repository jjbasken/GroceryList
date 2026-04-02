const SHELL_CACHE = 'grocery-v1';
const DATA_CACHE = 'grocery-data-v1';

const APP_SHELL = [
    '/',
    '/static/app.js',
    '/static/style.css',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(SHELL_CACHE).then(cache => cache.addAll(APP_SHELL))
    );
    self.skipWaiting();
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then(keys =>
            Promise.all(
                keys
                    .filter(k => k !== SHELL_CACHE && k !== DATA_CACHE)
                    .map(k => caches.delete(k))
            )
        ).then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const url = new URL(event.request.url);

    // Only handle same-origin requests
    if (url.origin !== self.location.origin) return;

    // App shell + static assets: cache-first
    if (url.pathname === '/' || url.pathname.startsWith('/static/')) {
        event.respondWith(
            caches.open(SHELL_CACHE).then(cache =>
                cache.match(event.request).then(cached => {
                    if (cached) return cached;
                    return fetch(event.request).then(response => {
                        if (response.ok) cache.put(event.request, response.clone());
                        return response;
                    });
                })
            )
        );
        return;
    }

    // API GET requests: network-first, fall back to cache
    if (event.request.method === 'GET' &&
        (url.pathname.startsWith('/api/items') || url.pathname.startsWith('/api/lists'))) {
        event.respondWith(
            fetch(event.request).then(response => {
                if (response.ok) {
                    caches.open(DATA_CACHE).then(cache => cache.put(event.request, response.clone()));
                }
                return response;
            }).catch(() =>
                caches.open(DATA_CACHE).then(cache =>
                    cache.match(event.request).then(
                        cached => cached || new Response('[]', { headers: { 'Content-Type': 'application/json' } })
                    )
                )
            )
        );
        return;
    }
});
