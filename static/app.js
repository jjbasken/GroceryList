/* global fetch, EventSource */
(function () {
    "use strict";

    const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

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

    // ---- API helpers ----

    async function api(url, opts = {}) {
        opts.headers = { "Content-Type": "application/json", "X-CSRFToken": csrfToken, ...opts.headers };
        const res = await fetch(url, opts);
        if (res.status === 401) {
            window.location.href = "/login";
            return null;
        }
        return res.json();
    }

    // ---- Lists ----

    async function loadLists() {
        const lists = await api("/api/lists");
        if (!lists || !lists.length) return;

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
        deleteListBtn.style.display = lists.length <= 1 ? "none" : "";
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

    function createItemEl(item) {
        const li = document.createElement("li");
        li.className = "item" + (item.is_bought ? " bought" : "");
        li.dataset.id = item.id;

        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = !!item.is_bought;
        cb.setAttribute("aria-label", "Mark " + item.name + " as bought");
        cb.addEventListener("change", () => toggleItem(item.id));

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

        const user = document.createElement("span");
        user.className = "item-user";
        user.textContent = item.added_by_name || "";

        const actions = document.createElement("span");
        actions.className = "item-actions";

        if (!item.is_bought) {
            const moveBtn = document.createElement("button");
            moveBtn.textContent = item.section === "now" ? "\u2935" : "\u2934";
            moveBtn.title = item.section === "now" ? "Move to Later" : "Move to Now";
            moveBtn.setAttribute("aria-label", moveBtn.title);
            moveBtn.addEventListener("click", () => moveItem(item.id));
            actions.appendChild(moveBtn);
        }

        const deleteBtn = document.createElement("button");
        deleteBtn.textContent = "\u00D7";
        deleteBtn.title = "Delete item";
        deleteBtn.className = "delete-btn";
        deleteBtn.setAttribute("aria-label", "Delete " + item.name);
        deleteBtn.addEventListener("click", () => deleteItem(item.id));
        actions.appendChild(deleteBtn);

        li.appendChild(cb);
        li.appendChild(content);
        li.appendChild(user);
        li.appendChild(actions);

        return li;
    }

    // ---- Actions ----

    async function loadItems() {
        if (!currentListId) return;
        const items = await api("/api/items?list_id=" + currentListId);
        if (items) renderItems(items);
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
        es.addEventListener("update", () => loadItems());
        es.onerror = () => {
            es.close();
            setTimeout(connectSSE, 3000);
        };
    }

    // ---- Init ----
    loadLists().then(() => loadItems());
    loadHistory();
    connectSSE();
})();
