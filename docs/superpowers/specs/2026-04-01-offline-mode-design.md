# Offline Mode Design

**Date:** 2026-04-01  
**Status:** Approved

## Summary

Add full offline support to GroceryList: the app loads from a cold start with no network, all item operations (add, toggle, move, delete, clear bought) work offline, and changes sync automatically when connectivity returns. No backend changes required.

---

## Architecture

Three pieces work together:

### 1. Service Worker (`static/sw.js`) — new file

Registered from `app.js` on page load.

- **Install**: Pre-caches the app shell — `/`, `/static/app.js`, `/static/style.css`, `/static/icon-192.png`, `/static/icon-512.png`, `/static/manifest.webmanifest`. Cache versioned as `grocery-v1`.
- **Activate**: Deletes old caches (any cache name not matching `grocery-v1`).
- **Fetch interception**:
  - Static assets (`/static/*`, `/`): cache-first.
  - `GET /api/items*` and `GET /api/lists`: network-first; on success, write response clone to `item-cache` in IndexedDB; on network failure, serve from `item-cache`.
  - All mutation requests: pass through network-only. The service worker does not touch mutations — `app.js` owns that logic.

### 2. IndexedDB (`grocery-offline`) — managed by `app.js`

Two object stores:

- **`item-cache`**: Snapshots of GET API responses. Key = URL string. Value = `{ data: [...], timestamp }`. Written on every successful GET; read when offline.
- **`op-queue`**: Ordered pending mutations. Auto-increment key. Value = `{ method, url, body }`. Written when a mutation is attempted offline; consumed during sync.

### 3. `app.js` changes

The existing `api()` helper is wrapped with offline awareness:

- **Mutation calls when offline**: Write to `op-queue`, update `localItems` optimistically, return synthetic `{ ok: true }` to the caller.
- **GET calls when offline**: Serve from `item-cache` (the service worker handles this via fetch interception, but `localItems` in memory is the primary source after first load).
- **`localItems` array**: Maintained in memory, mirroring server state. `loadItems()` populates it from the server when online; renders from it when offline.
- **Sync on reconnect**: `window.addEventListener('online', flushQueue)`. `flushQueue()` replays `op-queue` entries in order, then calls `loadItems()` to reconcile with server state.

---

## Optimistic UI

When a mutation happens offline, the UI updates immediately:

| Operation | Local effect |
|-----------|-------------|
| Add item | Temp ID (`"pending-" + Date.now()`), `pending: true` flag set, item rendered muted with no action buttons |
| Toggle bought | `is_bought` flipped in `localItems`, re-rendered |
| Move (now↔later) | `section` toggled in `localItems`, re-rendered |
| Delete | Item removed from `localItems`, re-rendered |
| Clear bought | Bought items removed from `localItems`, re-rendered |

Pending items (those with `pending: true`) have their toggle/move/delete buttons hidden to prevent dependency chains in the queue (e.g. deleting an item not yet created on the server). After sync, a fresh `loadItems()` resolves temp IDs.

---

## UI Indicators

- **Offline banner**: A strip at the top of the page shown when `navigator.onLine === false`, hidden when back online.
- **Syncing indicator**: Brief "Syncing…" text shown during `flushQueue()` execution.
- **Pending item style**: Items with `pending: true` are rendered at reduced opacity with action buttons hidden.

---

## Sync & Conflict Handling

Queue entries are replayed in insertion order.

| Server response | Handling |
|----------------|----------|
| 2xx | Success — remove entry from queue |
| 404 | Item already gone — skip silently |
| 4xx (other) | Operation no longer valid — skip silently |
| 401 | Session expired — preserve queue, redirect to `/login`; queue flushes on next `online` event after re-login |
| 5xx / network error | Halt flush, retry on next `online` event |

After a successful flush, `loadItems()` is called to reconcile local state with the server (resolves temp IDs, picks up changes from other users).

---

## Persistence Across Sessions

- `op-queue` survives tab close and browser restart (IndexedDB).
- `item-cache` survives tab close (IndexedDB + service worker cache).
- On cold start offline: service worker serves `/` and static assets from cache; `loadItems()` reads `item-cache` from IndexedDB and renders.
- On cold start online with a pending queue: queue flushes before first `loadItems()`.

---

## CSRF Token Handling

The CSRF token is embedded in the cached HTML page (meta tag). Flask-WTF defaults to a 1-hour token expiry, so a user offline for more than an hour could get CSRF 400 errors on sync.

**Resolution**: Set `WTF_CSRF_TIME_LIMIT = None` in `config.py`. This makes the token valid for the lifetime of the session (already up to 100 years per `app.permanent_session_lifetime`). This is the one required backend change.

The op-queue entries do not need to store the CSRF token — `app.js` reads it from `document.querySelector('meta[name="csrf-token"]').content` at flush time, which is the token from the (potentially cached) page load.

---

## Out of Scope

- List operations (create list, delete list) do not need to work offline.
- Conflict UI — no user-facing conflict resolution; server state wins after sync.
- Background sync (Background Sync API) — not used due to lack of Safari/iOS support.
- Backend changes beyond the one-liner CSRF fix.

---

## Files Changed

| File | Change |
|------|--------|
| `static/sw.js` | New — service worker |
| `static/app.js` | Modified — register SW, add IndexedDB helpers, offline-aware `api()`, `localItems` state, `flushQueue()`, offline banner |
| `templates/list.html` | Modified — add offline banner element |
| `templates/base.html` | Modified — add offline banner styles (or `style.css`) |
| `static/style.css` | Modified — offline banner + pending item styles |
| `config.py` | Modified — set `WTF_CSRF_TIME_LIMIT = None` |
