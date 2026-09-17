/* global fetch, EventSource, FileReader */
(function () {
    "use strict";

    let csrfToken = document.querySelector('meta[name="csrf-token"]').content;
    const accountId = document.querySelector('meta[name="account-id"]').content;
    const importUrlEnabled = document.querySelector('meta[name="import-url-enabled"]').content === "true";

    let recipes = [];

    // ---- API helper ----
    // No offline queue here (unlike app.js) -- recipe editing/importing/add-to-list
    // require connectivity. See the plan notes for why: reusing app.js's queue would
    // mean touching its security-sensitive account-isolation code for a feature that
    // doesn't need offline support.

    async function refreshCsrfToken() {
        try {
            const res = await fetch("/api/csrf-token", { headers: { "X-Account-ID": accountId } });
            if (res.status === 401 || res.status === 409) { window.location.href = "/login"; return false; }
            const data = await res.json();
            if (data && data.token) { csrfToken = data.token; return true; }
        } catch (e) { /* network error -- keep the old token */ }
        return false;
    }

    async function isCsrfFailure(res) {
        if (res.status !== 400) return false;
        const body = await res.clone().json().catch(() => null);
        return !!(body && body.error === "csrf");
    }

    async function api(url, opts = {}) {
        const doFetch = () => fetch(url, {
            ...opts,
            headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken, "X-Account-ID": accountId, ...opts.headers },
        });
        let res = await doFetch();
        const method = (opts.method || "GET").toUpperCase();
        if (method !== "GET" && await isCsrfFailure(res) && await refreshCsrfToken()) {
            res = await doFetch();
        }
        if (res.status === 401 || res.status === 409) {
            window.location.href = "/login";
            throw new Error("unauthorized");
        }
        return res;
    }

    // ---- Modal ----

    const overlay = document.getElementById("modal-overlay");
    const modal = document.getElementById("modal");

    function closeModal() {
        overlay.hidden = true;
        modal.innerHTML = "";
    }

    function openModal(html) {
        modal.innerHTML = html;
        overlay.hidden = false;
    }

    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !overlay.hidden) closeModal(); });

    function escapeHtml(str) {
        // Used both as element text and inside quoted HTML attributes (e.g. the
        // recipe-name input's value="..."), so quotes must be escaped too --
        // a textContent/innerHTML round trip alone would leave a raw `"` in a
        // recipe name free to break out of the attribute.
        return String(str == null ? "" : str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // ---- Recipe list ----

    const listEl = document.getElementById("recipe-list");
    const emptyMsg = document.getElementById("recipes-empty");

    async function loadRecipes() {
        const res = await api("/api/recipes");
        recipes = await res.json();
        renderRecipes();
    }

    function renderRecipes() {
        listEl.innerHTML = "";
        emptyMsg.hidden = recipes.length !== 0;
        recipes.forEach((r) => {
            const li = document.createElement("li");
            li.className = "recipe-card";
            li.innerHTML = `
                <div class="recipe-card-main">
                    <div class="recipe-card-name">${escapeHtml(r.name)}</div>
                    ${r.notes ? `<div class="recipe-card-notes">${escapeHtml(r.notes)}</div>` : ""}
                    <div class="recipe-card-meta">${r.ingredient_count} ingredient${r.ingredient_count === 1 ? "" : "s"}</div>
                </div>
                <div class="recipe-card-actions">
                    <button class="admin-action-btn btn-ok add-to-list-btn">Add to List</button>
                    <button class="admin-action-btn edit-btn">Edit</button>
                    ${r.can_delete ? '<button class="admin-action-btn btn-warn delete-btn">Delete</button>' : ""}
                </div>
            `;
            li.querySelector(".add-to-list-btn").addEventListener("click", () => openAddToListDialog(r.id));
            li.querySelector(".edit-btn").addEventListener("click", () => openRecipeDetailForEdit(r.id));
            const deleteBtn = li.querySelector(".delete-btn");
            if (deleteBtn) deleteBtn.addEventListener("click", () => deleteRecipe(r.id, r.name));
            listEl.appendChild(li);
        });
    }

    async function deleteRecipe(id, name) {
        if (!confirm(`Delete recipe "${name}"? This cannot be undone.`)) return;
        const res = await api(`/api/recipes/${id}`, { method: "DELETE" });
        if (res.ok) {
            loadRecipes();
        } else {
            const body = await res.json().catch(() => ({}));
            alert(body.error || "Could not delete recipe");
        }
    }

    // ---- Create / edit form ----

    function recipeFormHtml(prefill) {
        const name = (prefill && prefill.name) || "";
        const notes = (prefill && prefill.notes) || "";
        const ingredients = prefill && prefill.ingredients ? prefill.ingredients.map((i) => i.text).join("\n") : "";
        const steps = (prefill && prefill.steps) || "";
        return `
            <h2>${prefill && prefill.id ? "Edit Recipe" : "New Recipe"}</h2>
            <form id="recipe-form">
                <label>Name
                    <input type="text" id="recipe-name-input" value="${escapeHtml(name)}" required maxlength="200">
                </label>
                <label>Notes
                    <textarea id="recipe-notes-input" rows="2" placeholder="e.g. kids loved this one">${escapeHtml(notes)}</textarea>
                </label>
                <label>Ingredients (one per line)
                    <textarea id="recipe-ingredients-input" rows="6" required placeholder="2 cups flour&#10;1 egg">${escapeHtml(ingredients)}</textarea>
                </label>
                <label>Steps (one per line)
                    <textarea id="recipe-steps-input" rows="6" placeholder="Preheat oven to 350F&#10;Mix ingredients">${escapeHtml(steps)}</textarea>
                </label>
                <p id="recipe-form-error" class="modal-error" hidden></p>
                <div class="modal-actions">
                    <button type="button" id="recipe-form-cancel" class="admin-action-btn">Cancel</button>
                    <button type="submit" class="admin-action-btn btn-ok">Save</button>
                </div>
            </form>
        `;
    }

    function linesFrom(textarea) {
        return textarea.value.split("\n").map((s) => s.trim()).filter(Boolean);
    }

    function openRecipeForm(existingRecipe, prefill) {
        openModal(recipeFormHtml(prefill || existingRecipe));
        document.getElementById("recipe-form-cancel").addEventListener("click", closeModal);
        document.getElementById("recipe-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            const errorEl = document.getElementById("recipe-form-error");
            errorEl.hidden = true;
            const payload = {
                name: document.getElementById("recipe-name-input").value.trim(),
                notes: document.getElementById("recipe-notes-input").value.trim(),
                ingredients: linesFrom(document.getElementById("recipe-ingredients-input")),
                steps: linesFrom(document.getElementById("recipe-steps-input")),
            };
            const url = existingRecipe ? `/api/recipes/${existingRecipe.id}` : "/api/recipes";
            const res = await api(url, { method: existingRecipe ? "PUT" : "POST", body: JSON.stringify(payload) });
            if (res.ok) {
                closeModal();
                loadRecipes();
            } else {
                const body = await res.json().catch(() => ({}));
                errorEl.textContent = body.error || "Could not save recipe";
                errorEl.hidden = false;
            }
        });
    }

    async function openRecipeDetailForEdit(id) {
        const res = await api(`/api/recipes/${id}`);
        if (!res.ok) return;
        const recipe = await res.json();
        openRecipeForm(recipe);
    }

    // ---- Import ----

    function importChooserHtml() {
        return `
            <h2>Import Recipe</h2>
            ${importUrlEnabled ? `
            <div class="import-section">
                <label>From a URL
                    <input type="url" id="import-url-input" placeholder="https://example.com/recipe">
                </label>
                <button type="button" id="import-url-btn" class="admin-action-btn btn-ok">Fetch</button>
                <p id="import-url-status" class="modal-hint" hidden></p>
            </div>
            ` : '<p class="modal-hint">URL import is not configured on this server.</p>'}
            <div class="import-section">
                <label>From JSON
                    <textarea id="import-json-input" rows="5" placeholder='{"name": "...", "notes": "...", "ingredients": ["..."], "steps": ["..."]}'></textarea>
                </label>
                <input type="file" id="import-json-file" accept="application/json">
                <button type="button" id="import-json-btn" class="admin-action-btn btn-ok">Use JSON</button>
            </div>
            <p id="import-error" class="modal-error" hidden></p>
            <div class="modal-actions">
                <button type="button" id="import-cancel" class="admin-action-btn">Cancel</button>
            </div>
        `;
    }

    function showImportError(msg) {
        const el = document.getElementById("import-error");
        el.textContent = msg;
        el.hidden = false;
    }

    function validateImportedShape(data) {
        if (!data || typeof data !== "object") return null;
        const name = typeof data.name === "string" ? data.name.slice(0, 200) : "";
        const notes = typeof data.notes === "string" ? data.notes.slice(0, 2000) : "";
        const ingredients = Array.isArray(data.ingredients)
            ? data.ingredients.filter((i) => typeof i === "string" && i.trim()).slice(0, 200)
            : [];
        const steps = Array.isArray(data.steps)
            ? data.steps.filter((s) => typeof s === "string" && s.trim()).slice(0, 100)
            : [];
        if (!name || ingredients.length === 0) return null;
        return { name, notes, ingredients: ingredients.map((i) => ({ text: i })), steps: steps.join("\n") };
    }

    function openImportChooser() {
        openModal(importChooserHtml());
        document.getElementById("import-cancel").addEventListener("click", closeModal);

        const urlBtn = document.getElementById("import-url-btn");
        if (urlBtn) {
            urlBtn.addEventListener("click", async () => {
                const url = document.getElementById("import-url-input").value.trim();
                if (!url) return;
                const status = document.getElementById("import-url-status");
                status.hidden = false;
                status.textContent = "Fetching and extracting…";
                urlBtn.disabled = true;
                try {
                    const res = await api("/api/recipes/extract-url", { method: "POST", body: JSON.stringify({ url }) });
                    const body = await res.json().catch(() => ({}));
                    if (!res.ok) {
                        showImportError(body.error || "Could not import that recipe");
                        return;
                    }
                    openRecipeForm(null, {
                        name: body.name,
                        notes: body.notes,
                        ingredients: (body.ingredients || []).map((i) => ({ text: i })),
                        steps: (body.steps || []).join("\n"),
                    });
                } finally {
                    urlBtn.disabled = false;
                    status.hidden = true;
                }
            });
        }

        function useJsonText(text) {
            let parsed;
            try {
                parsed = JSON.parse(text);
            } catch (e) {
                showImportError("That is not valid JSON");
                return;
            }
            const prefill = validateImportedShape(parsed);
            if (!prefill) {
                showImportError('JSON must include a "name" and at least one ingredient');
                return;
            }
            openRecipeForm(null, prefill);
        }

        document.getElementById("import-json-btn").addEventListener("click", () => {
            const text = document.getElementById("import-json-input").value.trim();
            if (!text) { showImportError("Paste or choose a JSON file first"); return; }
            useJsonText(text);
        });

        document.getElementById("import-json-file").addEventListener("change", (e) => {
            const file = e.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = () => useJsonText(String(reader.result));
            reader.readAsText(file);
        });
    }

    // ---- Add to list ----

    async function openAddToListDialog(recipeId) {
        const [recipeRes, listsRes] = await Promise.all([
            api(`/api/recipes/${recipeId}`),
            api("/api/lists"),
        ]);
        if (!recipeRes.ok || !listsRes.ok) return;
        const recipe = await recipeRes.json();
        const lists = await listsRes.json();

        const checklistHtml = recipe.ingredients.map((ing, idx) => `
            <label class="checklist-row">
                <input type="checkbox" data-idx="${idx}" checked>
                <span>${escapeHtml(ing.text)}</span>
            </label>
        `).join("");
        const listOptions = lists.map((l) => `<option value="${l.id}">${escapeHtml(l.name)}</option>`).join("");

        openModal(`
            <h2>Add "${escapeHtml(recipe.name)}" to a list</h2>
            <div class="checklist">${checklistHtml}</div>
            <div class="add-to-list-controls">
                <select id="add-to-list-select">${listOptions}</select>
                <select id="add-to-list-section">
                    <option value="now">Now</option>
                    <option value="later">Later</option>
                </select>
            </div>
            <p id="add-to-list-error" class="modal-error" hidden></p>
            <div class="modal-actions">
                <button type="button" id="add-to-list-cancel" class="admin-action-btn">Cancel</button>
                <button type="button" id="add-to-list-confirm" class="admin-action-btn btn-ok">Add to List</button>
            </div>
        `);

        document.getElementById("add-to-list-cancel").addEventListener("click", closeModal);
        document.getElementById("add-to-list-confirm").addEventListener("click", async () => {
            const checked = Array.from(modal.querySelectorAll('.checklist input[type="checkbox"]:checked'))
                .map((cb) => recipe.ingredients[Number(cb.dataset.idx)].id);
            if (checked.length === 0) { closeModal(); return; }
            const listId = Number(document.getElementById("add-to-list-select").value);
            const section = document.getElementById("add-to-list-section").value;
            const confirmBtn = document.getElementById("add-to-list-confirm");
            confirmBtn.disabled = true;
            try {
                const res = await api(`/api/recipes/${recipeId}/add-to-list`, {
                    method: "POST",
                    body: JSON.stringify({ ingredient_ids: checked, section, list_id: listId }),
                });
                if (!res.ok) throw new Error("add_failed");
                closeModal();
            } catch (e) {
                const errorEl = document.getElementById("add-to-list-error");
                errorEl.hidden = false;
                errorEl.textContent = "Could not add ingredients -- check your connection and try again.";
                confirmBtn.disabled = false;
            }
        });
    }

    // ---- Events ----

    document.getElementById("new-recipe-btn").addEventListener("click", () => openRecipeForm(null));
    document.getElementById("import-recipe-btn").addEventListener("click", openImportChooser);

    // ---- SSE ----
    // Reuses the same "update" broadcast items/lists already trigger, so another
    // tab's recipe edits show up here too.

    function connectSSE() {
        const es = new EventSource("/api/stream");
        es.addEventListener("update", () => loadRecipes());
        es.onerror = () => { es.close(); setTimeout(connectSSE, 3000); };
    }

    // ---- Init ----
    loadRecipes();
    connectSSE();
})();
