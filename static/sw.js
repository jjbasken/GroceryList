importScripts('/static/offline-security.js');

const SHELL_CACHE = 'grocery-static-v8';
const SESSION_CACHE = 'grocery-session-v2';
const ACCOUNT_MARKER = '/__offline_account__';
const APP_SHELL = [
    '/static/app.js',
    '/static/offline-security.js',
    '/static/style.css',
    '/static/icon-192.png',
    '/static/icon-512.png',
    '/static/manifest.webmanifest',
];

// Serialize storage transitions, and prevent requests begun before logout from
// repopulating private caches after cleanup completes.
let generation = 0;
let storage = Promise.resolve();
function withStorage(fn) {
    const result = storage.then(fn);
    storage = result.catch(() => {});
    return result;
}
async function activeAccount() {
    const cache = await caches.open(SESSION_CACHE);
    const response = await cache.match(ACCOUNT_MARKER);
    return response ? response.text() : null;
}
function privateCache(account) {
    return 'grocery-private-v2-' + account;
}
async function selectAccount(account) {
    if (await activeAccount() !== account) {
        await GroceryOffline.clearPrivateData();
        const cache = await caches.open(SESSION_CACHE);
        await cache.put(ACCOUNT_MARKER, new Response(account));
    }
}

self.addEventListener('install', event => {
    event.waitUntil(caches.open(SHELL_CACHE).then(cache => cache.addAll(APP_SHELL)));
    self.skipWaiting();
});
self.addEventListener('activate', event => {
    // Legacy caches and unbound operations cannot be assigned to an account.
    event.waitUntil(withStorage(() => GroceryOffline.clearPrivateData())
        .then(() => self.clients.claim()));
});

self.addEventListener('fetch', event => {
    const url = new URL(event.request.url);
    if (url.origin !== self.location.origin) return;

    if (['/login', '/logout', '/register', '/change-password'].includes(url.pathname)) {
        generation++;
        event.respondWith(withStorage(() => GroceryOffline.clearPrivateData())
            .then(() => fetch(event.request)));
        return;
    }

    if (event.request.method === 'GET' && url.pathname.startsWith('/static/')) {
        event.respondWith(caches.open(SHELL_CACHE).then(async cache => {
            const cached = await cache.match(event.request);
            if (cached) return cached;
            const response = await fetch(event.request);
            if (response.ok) await cache.put(event.request, response.clone());
            return response;
        }));
        return;
    }

    const isPage = url.pathname === '/';
    const isData = url.pathname.startsWith('/api/items') || url.pathname === '/api/lists';
    if (event.request.method !== 'GET' || (!isPage && !isData)) return;

    const started = generation;
    const requestedAccount = event.request.headers.get('X-Account-ID');
    event.respondWith((async () => {
        let response;
        try {
            response = await fetch(event.request);
        } catch (error) {
            return withStorage(async () => {
                const account = await activeAccount();
                if (started === generation && account && (isPage || account === requestedAccount)) {
                    const cache = await caches.open(privateCache(account));
                    const cached = await cache.match(event.request);
                    if (cached) return cached;
                }
                return new Response('Offline data unavailable. Connect and sign in.', { status: 503 });
            });
        }
        const account = response.headers.get('X-Account-ID');
        // Never cache redirects to login or responses for a different account.
        if (response.ok && !response.redirected && account && (isPage || account === requestedAccount)) {
            const copy = response.clone();
            await withStorage(async () => {
                if (started !== generation) return;
                if (isPage) await selectAccount(account);
                if (await activeAccount() !== account) return;
                const cache = await caches.open(privateCache(account));
                await cache.put(event.request, copy);
            });
        } else if (response.status === 401 || response.redirected) {
            await withStorage(() => {
                if (started !== generation) return;
                generation++;
                return GroceryOffline.clearPrivateData();
            });
        }
        return response;
    })());
});
