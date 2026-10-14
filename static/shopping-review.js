/* global PageApi */
// The "add ingredients to a list" review sheet, shared by the recipes page,
// the meals page and the grocery list's "This week" strip.
//
// Nothing reaches the list unreviewed: the server previews the combined,
// scaled ingredients against the chosen list, the user ticks what they want,
// and only the confirmed lines are written (in one transaction).
(function () {
    "use strict";

    const LAST_LIST_KEY = "meals-last-list";
    const esc = (s) => PageApi.escapeHtml(s);

    function rememberedListId() {
        try { return Number(localStorage.getItem(LAST_LIST_KEY)) || null; } catch (e) { return null; }
    }

    function rememberList(id) {
        try { localStorage.setItem(LAST_LIST_KEY, String(id)); } catch (e) { /* private mode */ }
    }

    function statusHtml(line) {
        if (line.status === "on_list") {
            const had = line.existing_item.quantity ? ` (${esc(line.existing_item.quantity)})` : "";
            const becomes = line.combined_quantity && line.combined_quantity !== line.existing_item.quantity
                ? ` → ${esc(line.combined_quantity)}` : "";
            return `<span class="review-chip chip-on-list">On list${had}${becomes}</span>`;
        }
        if (line.status === "bought") return '<span class="review-chip chip-bought">Bought — still need?</span>';
        return "";
    }

    function lineHtml(line, idx) {
        const checked = line.action !== "skip" ? "checked" : "";
        return `
            <li class="review-line${line.action === "skip" ? " is-skipped" : ""}" data-idx="${idx}">
                <label class="review-check">
                    <input type="checkbox" ${checked} aria-label="Add ${esc(line.name)}">
                </label>
                <div class="review-body">
                    <div class="review-name">${esc(line.name)} ${statusHtml(line)}</div>
                    ${line.notes ? `<div class="review-notes">${esc(line.notes)}</div>` : ""}
                </div>
                <input type="text" class="review-qty" maxlength="50" value="${esc(line.quantity || "")}"
                       placeholder="Qty" aria-label="Quantity for ${esc(line.name)}">
            </li>`;
    }

    function noticesHtml(preview) {
        const notes = [];
        if (preview.already_added.length) {
            notes.push(`<p class="review-warning">Already added to a list: ${esc(preview.already_added.join(", "))}. Adding again may double up.</p>`);
        }
        if (preview.meals_without_recipe.length) {
            notes.push(`<p class="modal-hint">No recipe, so add these yourself: ${esc(preview.meals_without_recipe.join(", "))}</p>`);
        }
        if (preview.meals_skipped.length) {
            notes.push(`<p class="modal-hint">Leftovers and flexible nights don't add ingredients: ${esc(preview.meals_skipped.join(", "))}</p>`);
        }
        return notes.join("");
    }

    async function open(opts) {
        const { send, api, openModal, closeModal, toast } = PageApi;
        const listsRes = await api("/api/lists").catch(() => null);
        if (!listsRes || !listsRes.ok) { toast("Couldn't load your lists — check your connection."); return; }
        const lists = await listsRes.json();
        if (!lists.length) { toast("Create a grocery list first."); return; }
        const preferred = [opts.listId, rememberedListId()].find((id) => lists.some((l) => l.id === id));
        let listId = preferred || lists[0].id;
        let preview = null;

        const modal = openModal(`
            <h2>Add to list</h2>
            ${opts.title ? `<p class="modal-hint">${esc(opts.title)}</p>` : ""}
            <div class="review-controls">
                <select id="review-list" aria-label="List">
                    ${lists.map((l) => `<option value="${l.id}" ${l.id === listId ? "selected" : ""}>${esc(l.name)}</option>`).join("")}
                </select>
                <select id="review-section" aria-label="Section">
                    <option value="now">Now</option>
                    <option value="later">Later</option>
                </select>
            </div>
            <div id="review-content"><p class="modal-hint">Working out what you need…</p></div>
            <p id="review-error" class="modal-error" hidden></p>
            <div class="modal-actions">
                <button type="button" id="review-cancel" class="admin-action-btn">Cancel</button>
                <button type="button" id="review-confirm" class="admin-action-btn btn-ok" disabled>Add</button>
            </div>
        `);
        const content = modal.querySelector("#review-content");
        const confirmBtn = modal.querySelector("#review-confirm");
        const errorEl = modal.querySelector("#review-error");

        function showError(msg) {
            errorEl.textContent = msg;
            errorEl.hidden = !msg;
        }

        function selectedRows() {
            return Array.from(content.querySelectorAll(".review-line"))
                .filter((row) => row.querySelector('input[type="checkbox"]').checked);
        }

        function updateConfirm() {
            const n = selectedRows().length;
            confirmBtn.disabled = n === 0;
            confirmBtn.textContent = n ? `Add ${n} item${n === 1 ? "" : "s"}` : "Add";
        }

        async function loadPreview() {
            showError("");
            confirmBtn.disabled = true;
            const body = { list_id: listId };
            if (opts.recipeId) {
                body.recipe_id = opts.recipeId;
                body.servings = opts.servings || null;
            } else {
                body.meal_ids = opts.mealIds;
            }
            const { ok, data } = await send("/api/shopping/preview", "POST", body);
            if (!modal.isConnected || !content.isConnected) return;
            if (!ok) { content.innerHTML = ""; showError(data.error || "Couldn't work out the ingredients."); return; }
            preview = data;
            content.innerHTML = (data.lines.length
                ? `<ul class="review-lines">${data.lines.map(lineHtml).join("")}</ul>`
                : '<p class="empty-msg">No ingredients to add.</p>') + noticesHtml(data);
            content.querySelectorAll('.review-line input[type="checkbox"]').forEach((cb) => {
                cb.addEventListener("change", () => {
                    cb.closest(".review-line").classList.toggle("is-skipped", !cb.checked);
                    updateConfirm();
                });
            });
            updateConfirm();
        }

        modal.querySelector("#review-cancel").addEventListener("click", closeModal);
        modal.querySelector("#review-list").addEventListener("change", (e) => {
            listId = Number(e.target.value);
            loadPreview();
        });
        confirmBtn.addEventListener("click", async () => {
            const lines = selectedRows().map((row) => {
                const line = preview.lines[Number(row.dataset.idx)];
                // A bought match is unchecked by default; ticking it means "buy
                // it again", so it becomes a new item rather than a combine.
                const action = line.status === "on_list" ? "combine" : "add";
                return {
                    name: line.name,
                    quantity: row.querySelector(".review-qty").value.trim(),
                    notes: line.notes,
                    action,
                    existing_item_id: action === "combine" ? line.existing_item.id : undefined,
                };
            });
            confirmBtn.disabled = true;
            const { ok, data } = await send("/api/shopping/apply", "POST", {
                list_id: listId,
                section: modal.querySelector("#review-section").value,
                meal_ids: opts.mealIds || [],
                lines,
            });
            if (!ok) { showError(data.error || "Couldn't add the ingredients."); updateConfirm(); return; }
            rememberList(listId);
            closeModal();
            const listName = (lists.find((l) => l.id === listId) || {}).name || "the list";
            const parts = [`Added ${data.added}`];
            if (data.combined) parts.push(`combined ${data.combined}`);
            toast(`${parts.join(", ")} on ${listName}`);
            if (opts.onDone) opts.onDone(data, listId);
        });

        loadPreview();
    }

    window.ShoppingReview = { open };
})();
