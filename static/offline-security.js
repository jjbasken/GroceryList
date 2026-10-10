/* Shared by pages and the service worker. */
(function (scope) {
    'use strict';

    async function clearPrivateData({ legacyOnly = false } = {}) {
        await Promise.all([
            caches.keys().then(keys => Promise.all(keys
                // App-shell assets contain no account data and remain available
                // offline. The service worker removes superseded shell versions
                // during activation.
                .filter(key => key.startsWith('grocery-') && !key.startsWith('grocery-static-')
                    && (!legacyOnly || (!key.startsWith('grocery-private-v2-') && key !== 'grocery-session-v2')))
                .map(key => caches.delete(key)))),
            new Promise((resolve, reject) => {
                const request = indexedDB.open('grocery-offline', 1);
                request.onupgradeneeded = () => {
                    request.result.createObjectStore('op-queue', { autoIncrement: true });
                };
                request.onerror = () => reject(request.error);
                request.onsuccess = () => {
                    const db = request.result;
                    const tx = db.transaction('op-queue', 'readwrite');
                    const store = tx.objectStore('op-queue');
                    if (legacyOnly) {
                        const cursor = store.openCursor();
                        cursor.onsuccess = () => {
                            const row = cursor.result;
                            if (!row) return;
                            if (!row.value.accountId) row.delete();
                            row.continue();
                        };
                    } else {
                        store.clear();
                    }
                    tx.oncomplete = () => { db.close(); resolve(); };
                    tx.onerror = () => { db.close(); reject(tx.error); };
                };
            }),
        ]);
    }

    scope.GroceryOffline = { clearPrivateData };
    if (typeof document !== 'undefined') {
        document.addEventListener('submit', async event => {
            const form = event.target;
            if (new URL(form.action).pathname !== '/logout') return;
            event.preventDefault();
            // The worker clears again at the request boundary, fencing off
            // pending fetches in other tabs. This covers uncontrolled pages too.
            await clearPrivateData();
            form.submit();
        });
        // Do not restore a logged-out user's rendered page from the back cache.
        window.addEventListener('pageshow', event => {
            if (event.persisted) window.location.reload();
        });
    }
})(self);
