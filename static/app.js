/* global fetch, EventSource */
(function () {
    "use strict";

    const itemInput = document.getElementById("item-input");
    const sectionSelect = document.getElementById("section-select");
    const addBtn = document.getElementById("add-btn");
    const clearBoughtBtn = document.getElementById("clear-bought-btn");

    const sectionNowList = document.querySelector("#section-now .item-list");
    const sectionLaterList = document.querySelector("#section-later .item-list");
    const sectionNowCount = document.querySelector("#section-now .count");
    const sectionLaterCount = document.querySelector("#section-later .count");

    // ---- API helpers ----

    async function api(url, opts = {}) {
        opts.headers = { "Content-Type": "application/json", ...opts.headers };
        const res = await fetch(url, opts);
        if (res.status === 401) {
            window.location.href = "/login";
            return null;
        }
        return res.json();
    }

    // ---- Render ----

    function renderItems(items) {
        const now = items.filter(i => i.section === "now");
        const later = items.filter(i => i.section === "later");

        sectionNowCount.textContent = now.filter(i => !i.is_bought).length;
        sectionLaterCount.textContent = later.filter(i => !i.is_bought).length;

        sectionNowList.innerHTML = "";
        sectionLaterList.innerHTML = "";

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

        const name = document.createElement("span");
        name.className = "item-name";
        name.textContent = item.name;

        const user = document.createElement("span");
        user.className = "item-user";
        user.textContent = item.added_by_name;

        const actions = document.createElement("span");
        actions.className = "item-actions";

        const moveBtn = document.createElement("button");
        moveBtn.textContent = item.section === "now" ? "\u2935" : "\u2934";
        moveBtn.title = item.section === "now" ? "Move to Later" : "Move to Now";
        moveBtn.setAttribute("aria-label", moveBtn.title);
        moveBtn.addEventListener("click", () => moveItem(item.id));

        const delBtn = document.createElement("button");
        delBtn.className = "delete-btn";
        delBtn.textContent = "\u2715";
        delBtn.title = "Delete";
        delBtn.setAttribute("aria-label", "Delete " + item.name);
        delBtn.addEventListener("click", () => deleteItem(item.id));

        actions.appendChild(moveBtn);
        actions.appendChild(delBtn);

        li.appendChild(cb);
        li.appendChild(name);
        li.appendChild(user);
        li.appendChild(actions);

        return li;
    }

    // ---- Actions ----

    async function loadItems() {
        const items = await api("/api/items");
        if (items) renderItems(items);
    }

    async function addItem() {
        const name = itemInput.value.trim();
        if (!name) return;
        const section = sectionSelect.value;
        itemInput.value = "";
        await api("/api/items", {
            method: "POST",
            body: JSON.stringify({ name, section }),
        });
    }

    async function toggleItem(id) {
        await api("/api/items/" + id + "/toggle", { method: "POST" });
    }

    async function moveItem(id) {
        await api("/api/items/" + id + "/move", { method: "POST" });
    }

    async function deleteItem(id) {
        await api("/api/items/" + id, { method: "DELETE" });
    }

    async function clearBought() {
        await api("/api/items/clear-bought", { method: "POST" });
    }

    // ---- Events ----

    addBtn.addEventListener("click", addItem);
    itemInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") addItem();
    });
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
    loadItems();
    connectSSE();
})();
