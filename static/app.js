/* global fetch, EventSource */
(function () {
    "use strict";

    let csrfToken = document.querySelector('meta[name="csrf-token"]').content;
    const usernameMeta = document.querySelector('meta[name="username"]');
    const currentUsername = usernameMeta ? usernameMeta.content : '';

    const accountId = document.querySelector('meta[name="account-id"]').content;

    // ---- Local state ----
    let localItems = [];
    let localItemsDirty = false;

    // ---- IndexedDB helpers ----

    function openDB() {
        return new Promise((resolve, reject) => {
            const req = indexedDB.open('grocery-offline', 1);
            req.onupgradeneeded = (e) => {
                e.target.result.createObjectStore('op-queue', { autoIncrement: true });
            };
            req.onsuccess = (e) => resolve(e.target.result);
            req.onerror = (e) => reject(e.target.error);
        });
    }

    async function pushQueue(op) {
        const db = await openDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction('op-queue', 'readwrite');
            tx.objectStore('op-queue').add(op);
            tx.oncomplete = resolve;
            tx.onerror = (e) => reject(e.target.error);
        });
    }

    async function getAllQueue() {
        const db = await openDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction('op-queue', 'readonly');
            const store = tx.objectStore('op-queue');
            let ops = null, keys = null;
            const done = () => { if (ops !== null && keys !== null) resolve({ ops, keys }); };
            store.getAll().onsuccess = (e) => { ops = e.target.result; done(); };
            store.getAllKeys().onsuccess = (e) => { keys = e.target.result; done(); };
            tx.onerror = (e) => reject(e.target.error);
        });
    }

    async function deleteQueueEntry(key) {
        const db = await openDB();
        return new Promise((resolve, reject) => {
            const tx = db.transaction('op-queue', 'readwrite');
            tx.objectStore('op-queue').delete(key);
            tx.oncomplete = resolve;
            tx.onerror = (e) => reject(e.target.error);
        });
    }

    // ---- Optimistic local mutations ----

    function applyOptimistic(method, url, bodyStr) {
        localItemsDirty = true;
        const body = bodyStr ? JSON.parse(bodyStr) : {};
        if (method === 'POST' && url === '/api/items') {
            localItems.unshift({
                id: 'pending-' + Date.now(),
                name: body.name,
                section: body.section || 'now',
                quantity: body.quantity || null,
                notes: body.notes || null,
                is_bought: 0,
                list_id: body.list_id,
                added_by_name: currentUsername,
                pending: true,
            });
        } else if (method === 'POST' && /\/api\/items\/[^/]+\/toggle/.test(url)) {
            const idStr = url.match(/\/api\/items\/([^/]+)\/toggle/)[1];
            const item = localItems.find(i => String(i.id) === idStr);
            if (item) item.is_bought = item.is_bought ? 0 : 1;
        } else if (method === 'POST' && /\/api\/items\/[^/]+\/move/.test(url)) {
            const idStr = url.match(/\/api\/items\/([^/]+)\/move/)[1];
            const item = localItems.find(i => String(i.id) === idStr);
            if (item) item.section = item.section === 'now' ? 'later' : 'now';
        } else if (method === 'PUT' && /\/api\/items\/[^/]+$/.test(url)) {
            const idStr = url.match(/\/api\/items\/([^/]+)$/)[1];
            const item = localItems.find(i => String(i.id) === idStr);
            if (item) {
                item.name = body.name;
                item.quantity = body.quantity || null;
                item.notes = body.notes || null;
                item.section = body.section || item.section;
            }
        } else if (method === 'DELETE' && /\/api\/items\/[^/]+$/.test(url)) {
            const idStr = url.match(/\/api\/items\/([^/]+)$/)[1];
            localItems = localItems.filter(i => String(i.id) !== idStr);
        } else if (method === 'POST' && url === '/api/items/clear-bought') {
            localItems = localItems.filter(i => !i.is_bought);
        }
    }

    // ---- API helper ----

    async function refreshCsrfToken() {
        try {
            const res = await fetch('/api/csrf-token', { headers: { 'X-Account-ID': accountId } });
            if (res.status === 401 || res.status === 409) { window.location.href = '/login'; return false; }
            const data = await res.json();
            if (data && data.token) { csrfToken = data.token; return true; }
        } catch (e) { /* offline -- keep the old token */ }
        return false;
    }

    async function isCsrfFailure(res) {
        if (res.status !== 400) return false;
        const body = await res.clone().json().catch(() => null);
        return !!(body && body.error === 'csrf');
    }

    async function api(url, opts = {}) {
        const method = (opts.method || 'GET').toUpperCase();
        const doFetch = () => fetch(url, {
            ...opts,
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken, 'X-Account-ID': accountId, ...opts.headers },
        });
        const queueOp = async () => {
            await pushQueue({ accountId, method, url, body: opts.body || null });
            applyOptimistic(method, url, opts.body || null);
            return { ok: true };
        };

        if (method !== 'GET' && !navigator.onLine) return queueOp();

        let res;
        try {
            res = await doFetch();
        } catch (e) {
            // Network error while browser thinks we're online
            return method !== 'GET' ? queueOp() : null;
        }

        // Stale CSRF token (e.g. PWA resumed long after the page rendered):
        // grab a fresh token and retry once.
        if (method !== 'GET' && await isCsrfFailure(res) && await refreshCsrfToken()) {
            try {
                res = await doFetch();
            } catch (e) {
                return queueOp();
            }
        }

        if (res.status === 401 || res.status === 409) { window.location.href = '/login'; return null; }
        if (res.status === 403) {
            const error = await res.clone().json().catch(() => null);
            if (error && error.error === 'password_change_required') {
                window.location.href = '/change-password';
                return null;
            }
        }
        return res.json().catch(() => null);
    }

    // ---- Flush offline queue ----

    async function flushQueue() {
        const { ops, keys } = await getAllQueue();
        if (ops.length === 0) return;

        showSyncIndicator(true);
        for (let i = 0; i < ops.length; i++) {
            const op = ops[i];
            const key = keys[i];
            // Legacy operations have no identity and cannot safely be replayed.
            if (op.accountId !== accountId) {
                await deleteQueueEntry(key);
                continue;
            }
            const replay = () => fetch(op.url, {
                method: op.method,
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken, 'X-Account-ID': accountId },
                body: op.body || undefined,
            });
            try {
                let res = await replay();
                // Page token may be stale (queued ops can outlive a render);
                // refresh and retry once rather than dropping the write below.
                if (await isCsrfFailure(res) && await refreshCsrfToken()) {
                    res = await replay();
                }
                if (res.status === 401 || res.status === 409) {
                    showSyncIndicator(false);
                    window.location.href = '/login';
                    return;
                }
                if (res.status === 403) {
                    const error = await res.clone().json().catch(() => null);
                    if (error && error.error === 'password_change_required') {
                        window.location.href = '/change-password';
                        return;
                    }
                }
                if (res.status >= 500) break;
                // Remove on 2xx or 4xx (conflict/gone -- no retry for client errors)
                await deleteQueueEntry(key);
            } catch (e) {
                // Network error mid-flush: stop and retry on next online event
                break;
            }
        }
        localItemsDirty = false;
        showSyncIndicator(false);
        await loadLists();
        loadItems();
    }

    // ---- Offline UI ----

    const offlineBanner = document.getElementById('offline-banner');
    const syncIndicator = document.getElementById('sync-indicator');

    function updateOfflineBanner() {
        if (offlineBanner) offlineBanner.hidden = navigator.onLine;
    }

    function showSyncIndicator(show) {
        if (syncIndicator) syncIndicator.hidden = !show;
    }

    window.addEventListener('online', () => { updateOfflineBanner(); flushQueue(); });
    window.addEventListener('offline', updateOfflineBanner);

    // Flush queue and reload when returning from background or BFCache
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && navigator.onLine) flushQueue();
    });
    window.addEventListener('pageshow', (e) => {
        updateOfflineBanner();
        if (navigator.onLine) flushQueue();
        if (e.persisted) {
            // Restored from BFCache — reset stale in-memory state and reload
            localItems = [];
            localItemsDirty = false;
            loadLists().then(() => loadItems());
        }
    });

    // ---- Toggle controls ----
    const toggleControlsBtn = document.getElementById("toggle-controls-btn");
    const CONTROLS_KEY = "controls-collapsed";

    function applyControlsState(collapsed) {
        document.body.classList.toggle("controls-collapsed", collapsed);
        toggleControlsBtn.setAttribute("aria-label", collapsed ? "Show controls" : "Hide controls");
        toggleControlsBtn.title = collapsed ? "Show controls" : "Hide controls";
    }

    toggleControlsBtn.addEventListener("click", () => {
        const collapsed = !document.body.classList.contains("controls-collapsed");
        localStorage.setItem(CONTROLS_KEY, collapsed ? "1" : "0");
        applyControlsState(collapsed);
    });

    applyControlsState(localStorage.getItem(CONTROLS_KEY) === "1");

    const listSelect = document.getElementById("list-select");
    const newListBtn = document.getElementById("new-list-btn");
    const deleteListBtn = document.getElementById("delete-list-btn");
    const itemInput = document.getElementById("item-input");
    const qtyInput = document.getElementById("qty-input");
    const notesInput = document.getElementById("notes-input");
    const sectionSelect = document.getElementById("section-select");
    const addBtn = document.getElementById("add-btn");
    const clearBoughtBtn = document.getElementById("clear-bought-btn");
    const suggestionsDropdown = document.getElementById("item-suggestions");

    const sectionNowList = document.querySelector("#section-now .item-list");
    const sectionLaterList = document.querySelector("#section-later .item-list");
    const sectionBoughtList = document.querySelector("#section-bought .item-list");
    const sectionNowCount = document.querySelector("#section-now .count");
    const sectionLaterCount = document.querySelector("#section-later .count");
    const sectionBoughtCount = document.querySelector("#section-bought .count");
    const sectionBoughtEl = document.getElementById("section-bought");

    let currentListId = null;
    let currentLists = [];
    let editingId = null;

    // ---- Lists ----

    // The server enforces this too; hiding the button just avoids a dead control.
    function updateDeleteListBtn() {
        const current = currentLists.find(l => l.id === currentListId);
        const allowed = currentLists.length > 1 && current && current.can_delete;
        deleteListBtn.style.display = allowed ? "" : "none";
    }

    async function loadLists() {
        const lists = await api("/api/lists");
        if (!lists || !lists.length) return;
        currentLists = lists;

        listSelect.innerHTML = "";
        lists.forEach(l => {
            const opt = document.createElement("option");
            opt.value = l.id;
            opt.textContent = l.name;
            listSelect.appendChild(opt);
        });

        if (currentListId === null || !lists.find(l => l.id === currentListId)) {
            currentListId = lists[0].id;
        }
        listSelect.value = currentListId;
        updateDeleteListBtn();
    }

    async function createList() {
        const name = prompt("New list name:");
        if (!name || !name.trim()) return;
        const result = await api("/api/lists", {
            method: "POST",
            body: JSON.stringify({ name: name.trim() }),
        });
        if (result && result.id) {
            currentListId = result.id;
            await loadLists();
            loadItems();
        }
    }

    async function deleteList() {
        if (!currentListId) return;
        const listName = listSelect.options[listSelect.selectedIndex].text;
        if (!confirm("Delete \"" + listName + "\" and all its items?")) return;
        const result = await api("/api/lists/" + currentListId, { method: "DELETE" });
        if (result && result.ok) {
            currentListId = null;
            await loadLists();
            loadItems();
        } else if (result && result.error) {
            alert(result.error);
        }
    }

    // ---- Render ----

    function renderItems(items) {
        const now = items.filter(i => i.section === "now" && !i.is_bought);
        const later = items.filter(i => i.section === "later" && !i.is_bought);
        const bought = items.filter(i => i.is_bought);

        sectionNowCount.textContent = now.length;
        sectionLaterCount.textContent = later.length;
        sectionBoughtCount.textContent = bought.length;

        sectionNowList.innerHTML = "";
        sectionLaterList.innerHTML = "";
        sectionBoughtList.innerHTML = "";

        if (now.length === 0) {
            sectionNowList.innerHTML = '<li class="empty-msg">No items</li>';
        } else {
            now.forEach(i => sectionNowList.appendChild(createItemEl(i)));
        }

        if (later.length === 0) {
            sectionLaterList.innerHTML = '<li class="empty-msg">No items</li>';
        } else {
            later.forEach(i => sectionLaterList.appendChild(createItemEl(i)));
        }

        sectionBoughtEl.style.display = bought.length === 0 ? "none" : "";
        bought.forEach(i => sectionBoughtList.appendChild(createItemEl(i)));
    }

    function closeItemMenu() {
        const open = document.querySelector(".item-menu.open");
        if (open) {
            open.classList.remove("open");
            const btn = open.querySelector(".menu-btn");
            if (btn) btn.setAttribute("aria-expanded", "false");
        }
    }

    function toggleItemMenu(menu) {
        const wasOpen = menu.classList.contains("open");
        closeItemMenu();
        if (!wasOpen) {
            menu.classList.add("open");
            const btn = menu.querySelector(".menu-btn");
            if (btn) btn.setAttribute("aria-expanded", "true");
        }
    }

    document.addEventListener("click", closeItemMenu);

    function createItemEl(item) {
        if (editingId !== null && String(editingId) === String(item.id)) {
            return createEditEl(item);
        }

        const li = document.createElement("li");
        li.className = "item" + (item.is_bought ? " bought" : "") + (item.pending ? " pending" : "");
        li.dataset.id = item.id;

        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = !!item.is_bought;
        cb.setAttribute("aria-label", "Mark " + item.name + " as bought");
        if (item.pending) {
            cb.disabled = true;
        } else {
            cb.addEventListener("change", () => toggleItem(item.id));
        }

        const content = document.createElement("div");
        content.className = "item-content";

        const nameRow = document.createElement("div");
        nameRow.className = "item-name-row";

        const nameSpan = document.createElement("span");
        nameSpan.className = "item-name";
        nameSpan.textContent = item.name;
        nameRow.appendChild(nameSpan);

        if (item.quantity) {
            const qtySpan = document.createElement("span");
            qtySpan.className = "item-qty";
            qtySpan.textContent = item.quantity;
            nameRow.appendChild(qtySpan);
        }
        content.appendChild(nameRow);

        if (item.notes) {
            const notesSpan = document.createElement("span");
            notesSpan.className = "item-notes";
            notesSpan.textContent = item.notes;
            content.appendChild(notesSpan);
        }

        const actions = document.createElement("span");
        actions.className = "item-actions";

        if (!item.pending) {
            const menu = document.createElement("div");
            menu.className = "item-menu";

            const menuBtn = document.createElement("button");
            menuBtn.className = "menu-btn";
            menuBtn.textContent = "\u22ee";
            menuBtn.title = "More options";
            menuBtn.setAttribute("aria-label", "More options for " + item.name);
            menuBtn.setAttribute("aria-haspopup", "true");
            menuBtn.setAttribute("aria-expanded", "false");

            const dropdown = document.createElement("div");
            dropdown.className = "item-menu-dropdown";

            if (item.added_by_name) {
                const addedBy = document.createElement("div");
                addedBy.className = "item-menu-added-by";
                addedBy.textContent = item.added_by_name;
                dropdown.appendChild(addedBy);
            }

            const addAction = (label, className, onClick) => {
                const btn = document.createElement("button");
                btn.textContent = label;
                if (className) btn.className = className;
                btn.addEventListener("click", () => {
                    closeItemMenu();
                    onClick();
                });
                dropdown.appendChild(btn);
            };

            if (!item.is_bought) {
                addAction(item.section === "now" ? "Move to Later" : "Move to Now",
                    "", () => moveItem(item.id));
            }
            addAction("Edit", "edit-btn", () => startEdit(item));
            addAction("Delete", "delete-btn", () => deleteItem(item.id));

            menuBtn.addEventListener("click", (e) => {
                e.stopPropagation();
                toggleItemMenu(menu);
            });

            menu.appendChild(menuBtn);
            menu.appendChild(dropdown);
            actions.appendChild(menu);
        }

        li.appendChild(cb);
        li.appendChild(content);
        li.appendChild(actions);

        return li;
    }

    function createEditEl(item) {
        const li = document.createElement("li");
        li.className = "item item-editing";
        li.dataset.id = item.id;

        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.className = "edit-name";
        nameInput.value = item.name;
        nameInput.setAttribute("aria-label", "Item name");

        const qtyInput = document.createElement("input");
        qtyInput.type = "text";
        qtyInput.className = "edit-qty";
        qtyInput.value = item.quantity || "";
        qtyInput.placeholder = "Qty";
        qtyInput.setAttribute("aria-label", "Quantity");

        const sectionSel = document.createElement("select");
        sectionSel.className = "edit-section";
        [["now", "Now"], ["later", "Later"]].forEach(([value, label]) => {
            const opt = document.createElement("option");
            opt.value = value;
            opt.textContent = label;
            if (item.section === value) opt.selected = true;
            sectionSel.appendChild(opt);
        });

        const notesInput = document.createElement("input");
        notesInput.type = "text";
        notesInput.className = "edit-notes";
        notesInput.value = item.notes || "";
        notesInput.placeholder = "Notes (optional)";
        notesInput.setAttribute("aria-label", "Notes");

        const saveBtn = document.createElement("button");
        saveBtn.className = "edit-save";
        saveBtn.textContent = "Save";

        const cancelBtn = document.createElement("button");
        cancelBtn.className = "edit-cancel";
        cancelBtn.textContent = "Cancel";

        const save = () => {
            const name = nameInput.value.trim();
            if (!name) { nameInput.focus(); return; }
            editingId = null;
            saveEdit(item.id, {
                name,
                quantity: qtyInput.value.trim() || null,
                notes: notesInput.value.trim() || null,
                section: sectionSel.value,
            });
        };
        const cancel = () => { editingId = null; loadItems(); };

        saveBtn.addEventListener("click", save);
        cancelBtn.addEventListener("click", cancel);
        [nameInput, qtyInput, notesInput].forEach(inp => {
            inp.addEventListener("keydown", (e) => {
                if (e.key === "Enter") save();
                if (e.key === "Escape") cancel();
            });
        });

        const row1 = document.createElement("div");
        row1.className = "edit-row";
        row1.appendChild(nameInput);
        row1.appendChild(qtyInput);
        row1.appendChild(sectionSel);

        const row2 = document.createElement("div");
        row2.className = "edit-row";
        row2.appendChild(notesInput);
        row2.appendChild(saveBtn);
        row2.appendChild(cancelBtn);

        li.appendChild(row1);
        li.appendChild(row2);

        return li;
    }

    function startEdit(item) {
        editingId = item.id;
        renderItems(localItems);
        const li = document.querySelector(".item-editing");
        if (li) {
            const nameInput = li.querySelector(".edit-name");
            if (nameInput) { nameInput.focus(); nameInput.select(); }
        }
    }

    // ---- Actions ----

    async function loadItems() {
        if (!currentListId) return;
        // Don't clobber an in-progress inline edit with a background refresh
        if (editingId !== null) return;
        // If we have unsynced optimistic mutations, render from local state
        // rather than overwriting it with a (potentially stale) cached response
        if (localItemsDirty) {
            renderItems(localItems);
            return;
        }
        const items = await api("/api/items?list_id=" + currentListId);
        if (items) {
            localItems = items;
        }
        renderItems(localItems);
    }

    async function addItem() {
        const name = itemInput.value.trim();
        if (!name) return;
        const section = sectionSelect.value;
        const quantity = qtyInput.value.trim() || null;
        const notes = notesInput.value.trim() || null;
        itemInput.value = "";
        qtyInput.value = "";
        notesInput.value = "";
        await api("/api/items", {
            method: "POST",
            body: JSON.stringify({ name, section, quantity, notes, list_id: currentListId }),
        });
        loadItems();
        loadHistory();
    }

    async function toggleItem(id) {
        await api("/api/items/" + id + "/toggle", { method: "POST" });
        loadItems();
    }

    async function moveItem(id) {
        await api("/api/items/" + id + "/move", { method: "POST" });
        loadItems();
    }

    async function deleteItem(id) {
        await api("/api/items/" + id, { method: "DELETE" });
        loadItems();
    }

    async function saveEdit(id, payload) {
        await api("/api/items/" + id, {
            method: "PUT",
            body: JSON.stringify(payload),
        });
        loadItems();
        loadHistory();
    }

    async function clearBought() {
        await api("/api/items/clear-bought", {
            method: "POST",
            body: JSON.stringify({ list_id: currentListId }),
        });
        loadItems();
    }

    // ---- History (autocomplete) ----

    let allHistory = [];

    async function loadHistory() {
        const names = await api("/api/items/history");
        if (!names) return;
        allHistory = names;
    }

    function showSuggestions(query) {
        const q = query.trim().toLowerCase();
        const matches = q ? allHistory.filter(n => n.toLowerCase().includes(q)) : [];
        suggestionsDropdown.innerHTML = "";
        if (matches.length === 0) {
            suggestionsDropdown.hidden = true;
            return;
        }
        matches.forEach(name => {
            const row = document.createElement("div");
            row.className = "suggestion-row";

            const label = document.createElement("span");
            label.className = "suggestion-label";
            label.textContent = name;
            label.addEventListener("mousedown", (e) => {
                e.preventDefault();
                itemInput.value = name;
                suggestionsDropdown.hidden = true;
            });

            const removeBtn = document.createElement("button");
            removeBtn.className = "suggestion-remove";
            removeBtn.textContent = "\u00D7";
            removeBtn.title = "Remove from suggestions";
            removeBtn.setAttribute("aria-label", "Remove " + name + " from suggestions");
            removeBtn.addEventListener("mousedown", async (e) => {
                e.preventDefault();
                await api("/api/items/history/" + encodeURIComponent(name), { method: "DELETE" });
                allHistory = allHistory.filter(n => n !== name);
                showSuggestions(itemInput.value);
            });

            row.appendChild(label);
            row.appendChild(removeBtn);
            suggestionsDropdown.appendChild(row);
        });
        suggestionsDropdown.hidden = false;
    }

    // ---- Events ----

    listSelect.addEventListener("change", () => {
        currentListId = parseInt(listSelect.value, 10);
        updateDeleteListBtn();
        // Clear local state when switching lists so loadItems fetches fresh
        localItems = [];
        localItemsDirty = false;
        loadItems();
    });
    newListBtn.addEventListener("click", createList);
    deleteListBtn.addEventListener("click", deleteList);
    addBtn.addEventListener("click", addItem);
    itemInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { suggestionsDropdown.hidden = true; addItem(); }
        if (e.key === "Escape") { suggestionsDropdown.hidden = true; }
    });
    itemInput.addEventListener("input", () => showSuggestions(itemInput.value));
    itemInput.addEventListener("focus", () => showSuggestions(itemInput.value));
    itemInput.addEventListener("blur", () => { suggestionsDropdown.hidden = true; });
    clearBoughtBtn.addEventListener("click", clearBought);

    // ---- SSE ----

    function connectSSE() {
        const es = new EventSource("/api/stream");
        // Skip SSE-triggered reloads while we have unsynced local mutations
        es.addEventListener("update", () => { if (!localItemsDirty) loadItems(); });
        es.onerror = () => {
            es.close();
            setTimeout(connectSSE, 3000);
        };
    }

    // ---- Service worker ----

    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js');
    }

    // ---- Init ----
    updateOfflineBanner();
    loadLists().then(() => loadItems());
    loadHistory();
    connectSSE();
    // Replay any ops queued during a previous offline session
    if (navigator.onLine) flushQueue();

})();
