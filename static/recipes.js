/* global EventSource, FileReader, PageApi, ShoppingReview */
(function () {
    "use strict";

    // No offline queue here (unlike app.js) -- recipe editing, importing and
    // adding to a list require connectivity, which keeps this page out of the
    // security-sensitive account-isolation code in app.js.

    const { api, send, escapeHtml, openModal, closeModal, toast, dates } = PageApi;
    const importUrlEnabled = document.querySelector('meta[name="import-url-enabled"]').content === "true";
    const recipeId = Number(document.querySelector('meta[name="recipe-id"]').content) || null;

    const MAX_SERVINGS = 100;

    function safeHref(url) {
        // Only http(s) links are rendered, so a stored javascript: URL can't run.
        try {
            const parsed = new URL(url);
            return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
        } catch (e) {
            return null;
        }
    }

    // ---- Library ----

    const libraryView = document.getElementById("library-view");
    const listEl = document.getElementById("recipe-list");
    const emptyMsg = document.getElementById("recipes-empty");
    const searchInput = document.getElementById("recipe-search");
    let searchTimer = null;
    let loadSeq = 0;

    async function loadRecipes() {
        const query = searchInput.value.trim();
        const seq = ++loadSeq;
        let recipes;
        try {
            const res = await api("/api/recipes" + (query ? `?q=${encodeURIComponent(query)}` : ""));
            if (!res.ok) throw new Error("load_failed");
            recipes = await res.json();
        } catch (e) {
            if (seq !== loadSeq) return;
            listEl.innerHTML = "";
            emptyMsg.textContent = "Couldn't load recipes — check your connection.";
            emptyMsg.hidden = false;
            return;
        }
        // A slower, older search must not overwrite a newer one.
        if (seq !== loadSeq) return;
        renderRecipes(recipes, query);
    }

    function renderRecipes(recipes, query) {
        listEl.innerHTML = "";
        emptyMsg.hidden = recipes.length !== 0;
        emptyMsg.textContent = query
            ? `No recipes match "${query}".`
            : "No recipes yet — add one to get started.";
        recipes.forEach((r) => {
            const li = document.createElement("li");
            const count = r.ingredient_count;
            li.innerHTML = `
                <a class="recipe-card" href="/recipes/${r.id}">
                    <span class="recipe-card-name">${escapeHtml(r.name)}</span>
                    ${r.notes ? `<span class="recipe-card-notes">${escapeHtml(r.notes)}</span>` : ""}
                    <span class="recipe-card-meta">Serves ${r.servings} · ${count} ingredient${count === 1 ? "" : "s"}</span>
                </a>
            `;
            listEl.appendChild(li);
        });
    }

    // ---- Detail ----

    const detailView = document.getElementById("detail-view");
    let recipe = null;
    let servings = null;

    async function loadDetail() {
        const query = servings ? `?servings=${servings}` : "";
        let res;
        try {
            res = await api(`/api/recipes/${recipeId}${query}`);
        } catch (e) {
            renderDetailMessage("Couldn't load this recipe — check your connection.");
            return;
        }
        if (res.status === 404) {
            renderDetailMessage("This recipe doesn't exist any more. It may have been deleted.");
            return;
        }
        if (!res.ok) {
            renderDetailMessage("Couldn't load this recipe.");
            return;
        }
        recipe = await res.json();
        servings = recipe.scaled_servings;
        renderDetail();
    }

    function renderDetailMessage(message) {
        detailView.innerHTML = `
            <a class="back-link" href="/recipes">‹ All recipes</a>
            <p class="empty-msg">${escapeHtml(message)}</p>
        `;
    }

    function renderDetail() {
        const r = recipe;
        document.title = `${r.name} · GroceryList`;
        const sourceHref = r.source_url ? safeHref(r.source_url) : null;
        const steps = (r.steps || "").split("\n").filter((s) => s.trim());
        const ingredients = r.ingredients.map((i) => `
            <li>
                ${i.amount ? `<strong>${escapeHtml(i.amount)}</strong> ` : ""}${escapeHtml(i.name)}${i.note ? `<span class="muted">, ${escapeHtml(i.note)}</span>` : ""}
            </li>
        `).join("");
        detailView.innerHTML = `
            <a class="back-link" href="/recipes">‹ All recipes</a>
            <h2 class="recipe-title">${escapeHtml(r.name)}</h2>
            ${r.notes ? `<p class="recipe-notes">${escapeHtml(r.notes)}</p>` : ""}
            <div class="recipe-actions">
                <button type="button" id="add-to-list-btn" class="admin-action-btn btn-ok">Add to list</button>
                <button type="button" id="plan-it-btn" class="admin-action-btn">Plan it</button>
                <button type="button" id="edit-btn" class="admin-action-btn">Edit</button>
                ${r.can_delete ? '<button type="button" id="delete-btn" class="admin-action-btn btn-warn">Delete</button>' : ""}
            </div>
            <div class="servings-stepper">
                <span class="servings-label">Servings</span>
                <button type="button" id="servings-down" class="stepper-btn" aria-label="Fewer servings"${servings <= 1 ? " disabled" : ""}>−</button>
                <span class="servings-n" aria-live="polite">${servings}</span>
                <button type="button" id="servings-up" class="stepper-btn" aria-label="More servings"${servings >= MAX_SERVINGS ? " disabled" : ""}>+</button>
                ${servings !== r.servings ? `<span class="servings-hint">recipe makes ${r.servings}</span>` : ""}
            </div>
            <h3 class="recipe-subtitle">Ingredients</h3>
            <ul class="recipe-ingredients">${ingredients}</ul>
            <h3 class="recipe-subtitle">Steps</h3>
            ${steps.length
                ? `<ol class="recipe-steps">${steps.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ol>`
                : '<p class="muted">No steps yet.</p>'}
            ${sourceHref ? `<p class="recipe-source"><a href="${escapeHtml(sourceHref)}" target="_blank" rel="noopener noreferrer">Original recipe ↗</a></p>` : ""}
            ${r.created_by_name ? `<p class="muted recipe-byline">Added by ${escapeHtml(r.created_by_name)}</p>` : ""}
        `;

        document.getElementById("servings-down").addEventListener("click", () => changeServings(-1));
        document.getElementById("servings-up").addEventListener("click", () => changeServings(1));
        document.getElementById("add-to-list-btn").addEventListener("click", () => {
            ShoppingReview.open({ recipeId: r.id, servings, title: r.name });
        });
        document.getElementById("plan-it-btn").addEventListener("click", openPlanIt);
        document.getElementById("edit-btn").addEventListener("click", () => openRecipeForm(r));
        const deleteBtn = document.getElementById("delete-btn");
        if (deleteBtn) deleteBtn.addEventListener("click", deleteRecipe);
    }

    function changeServings(delta) {
        const next = Math.min(MAX_SERVINGS, Math.max(1, servings + delta));
        if (next === servings) return;
        servings = next;
        loadDetail();
    }

    async function deleteRecipe() {
        if (!confirm(`Delete "${recipe.name}"? This cannot be undone. Meals already planned with it keep their name.`)) return;
        const { ok, data } = await send(`/api/recipes/${recipe.id}`, "DELETE");
        if (ok) {
            window.location.href = "/recipes";
        } else {
            alert(data.error || "Could not delete recipe");
        }
    }

    // ---- Plan it ----

    function openPlanIt() {
        const modal = openModal(`
            <h2>Plan "${escapeHtml(recipe.name)}"</h2>
            <form id="plan-form">
                <label>Day
                    <input type="date" id="plan-date" value="${dates.todayISO()}" required>
                </label>
                <label>Servings
                    <input type="number" id="plan-servings" min="1" max="${MAX_SERVINGS}" step="1" inputmode="numeric" value="${servings}" required>
                </label>
                <p id="plan-error" class="modal-error" hidden></p>
                <div class="modal-actions">
                    <button type="button" id="plan-cancel" class="admin-action-btn">Cancel</button>
                    <button type="submit" class="admin-action-btn btn-ok">Plan it</button>
                </div>
            </form>
        `);
        modal.querySelector("#plan-cancel").addEventListener("click", closeModal);
        modal.querySelector("#plan-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            const errorEl = modal.querySelector("#plan-error");
            const date = modal.querySelector("#plan-date").value;
            const planServings = Number(modal.querySelector("#plan-servings").value);
            errorEl.hidden = true;
            const { ok, data } = await send("/api/meals", "POST", {
                date, kind: "cook", recipe_id: recipe.id,
                servings: Number.isInteger(planServings) ? planServings : null,
            });
            if (!ok) {
                errorEl.textContent = data.error || "Couldn't plan this meal";
                errorEl.hidden = false;
                return;
            }
            closeModal();
            toast(`Planned for ${dates.dayLabel(date)} — see it under Meals`);
        });
    }

    // ---- Create / edit form ----

    function ingredientRowHtml(ing) {
        const i = ing || {};
        // Stored amounts are floats; round so ⅓ cup edits as 0.333, not 0.3333333.
        const qty = i.qty_text != null ? i.qty_text
            : (i.qty == null || i.qty === "" ? "" : (typeof i.qty === "number" ? String(Math.round(i.qty * 1000) / 1000) : String(i.qty)));
        return `
            <div class="ingredient-row">
                <input type="text" class="ing-name" placeholder="Ingredient" aria-label="Ingredient" maxlength="200" value="${escapeHtml(i.name)}">
                <div class="ingredient-row-details">
                    <input type="text" class="ing-qty" placeholder="Amount" aria-label="Amount" inputmode="decimal" maxlength="20" value="${escapeHtml(qty)}">
                    <input type="text" class="ing-unit" placeholder="Unit" aria-label="Unit" maxlength="40" value="${escapeHtml(i.unit)}">
                    <input type="text" class="ing-note" placeholder="Note" aria-label="Note" maxlength="500" value="${escapeHtml(i.note)}">
                    <button type="button" class="ing-remove" aria-label="Remove ingredient" title="Remove">✕</button>
                </div>
            </div>
        `;
    }

    function recipeFormHtml(prefill, isEdit) {
        const p = prefill || {};
        const ingredients = p.ingredients && p.ingredients.length ? p.ingredients : [{}];
        return `
            <h2>${isEdit ? "Edit Recipe" : "New Recipe"}</h2>
            <form id="recipe-form" novalidate>
                <label>Name
                    <input type="text" id="recipe-name-input" value="${escapeHtml(p.name)}" required maxlength="200">
                </label>
                <div class="form-row">
                    <label>Servings
                        <input type="number" id="recipe-servings-input" min="1" max="${MAX_SERVINGS}" step="1" inputmode="numeric" value="${escapeHtml(p.servings || 4)}">
                    </label>
                    <label class="grow">Source link
                        <input type="url" id="recipe-source-input" placeholder="https://…" maxlength="2048" value="${escapeHtml(p.source_url)}">
                    </label>
                </div>
                <label>Notes
                    <textarea id="recipe-notes-input" rows="2" placeholder="e.g. kids loved this one">${escapeHtml(p.notes)}</textarea>
                </label>
                <fieldset class="ingredients-fieldset">
                    <legend>Ingredients</legend>
                    <div id="ingredient-rows">${ingredients.map(ingredientRowHtml).join("")}</div>
                    <button type="button" id="add-ingredient-btn" class="admin-action-btn">+ Add ingredient</button>
                    <details class="paste-ingredients">
                        <summary>Paste a list of ingredients</summary>
                        <textarea id="paste-ingredients-input" rows="4" placeholder="2 cups flour&#10;1 1/2 tsp salt&#10;2 (14 oz) cans tomatoes"></textarea>
                        <button type="button" id="paste-ingredients-btn" class="admin-action-btn">Add these</button>
                    </details>
                </fieldset>
                <label>Steps (one per line)
                    <textarea id="recipe-steps-input" rows="6" placeholder="Preheat oven to 350F&#10;Mix ingredients">${escapeHtml(p.steps)}</textarea>
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

    // existingRecipe is the saved recipe being edited (null for new); prefill
    // supplies field values, e.g. from an import.
    function openRecipeForm(existingRecipe, prefill) {
        const modal = openModal(recipeFormHtml(prefill || existingRecipe, !!existingRecipe));
        const rows = modal.querySelector("#ingredient-rows");
        const errorEl = modal.querySelector("#recipe-form-error");

        function showError(message) {
            errorEl.textContent = message;
            errorEl.hidden = false;
        }

        rows.addEventListener("click", (e) => {
            const remove = e.target.closest(".ing-remove");
            if (!remove) return;
            remove.closest(".ingredient-row").remove();
            if (!rows.children.length) rows.insertAdjacentHTML("beforeend", ingredientRowHtml());
        });
        modal.querySelector("#add-ingredient-btn").addEventListener("click", () => {
            rows.insertAdjacentHTML("beforeend", ingredientRowHtml());
            rows.lastElementChild.querySelector(".ing-name").focus();
        });
        modal.querySelector("#paste-ingredients-btn").addEventListener("click", async () => {
            const input = modal.querySelector("#paste-ingredients-input");
            const lines = linesFrom(input);
            if (!lines.length) return;
            errorEl.hidden = true;
            const { ok, data } = await send("/api/recipes/parse-ingredients", "POST", { lines });
            if (!ok) { showError(data.error || "Couldn't read those ingredients"); return; }
            // Replace the single blank starter row rather than leaving it dangling.
            Array.from(rows.querySelectorAll(".ingredient-row")).forEach((row) => {
                if (Array.from(row.querySelectorAll("input")).every((el) => !el.value.trim())) row.remove();
            });
            rows.insertAdjacentHTML("beforeend", data.ingredients.map(ingredientRowHtml).join(""));
            input.value = "";
            modal.querySelector(".paste-ingredients").open = false;
        });
        modal.querySelector("#recipe-form-cancel").addEventListener("click", closeModal);
        modal.querySelector("#recipe-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            errorEl.hidden = true;
            const servingsValue = Number(modal.querySelector("#recipe-servings-input").value);
            const payload = {
                name: modal.querySelector("#recipe-name-input").value.trim(),
                servings: Number.isInteger(servingsValue) ? servingsValue : 0,
                source_url: modal.querySelector("#recipe-source-input").value.trim() || null,
                notes: modal.querySelector("#recipe-notes-input").value.trim(),
                ingredients: Array.from(rows.querySelectorAll(".ingredient-row")).map((row) => ({
                    name: row.querySelector(".ing-name").value.trim(),
                    qty: row.querySelector(".ing-qty").value.trim() || null,
                    unit: row.querySelector(".ing-unit").value.trim(),
                    note: row.querySelector(".ing-note").value.trim(),
                })).filter((i) => i.name || i.qty || i.unit || i.note),
                steps: linesFrom(modal.querySelector("#recipe-steps-input")),
            };
            if (!payload.name) { showError("Name is required"); return; }
            const url = existingRecipe ? `/api/recipes/${existingRecipe.id}` : "/api/recipes";
            const { ok, data } = await send(url, existingRecipe ? "PUT" : "POST", payload);
            if (!ok) { showError(data.error || "Could not save recipe"); return; }
            closeModal();
            if (existingRecipe) {
                loadDetail();
            } else {
                window.location.href = `/recipes/${data.id}`;
            }
        });
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
                    <textarea id="import-json-input" rows="5" placeholder='{"name": "...", "servings": 4, "ingredients": ["2 cups flour", ...], "steps": ["..."]}'></textarea>
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

    // Turns imported JSON into editor prefill. String ingredients go through the
    // server's parser so the rows come back split into amount/unit/name.
    async function prefillFromImport(data) {
        if (!data || typeof data !== "object") return { error: 'JSON must include a "name" and at least one ingredient' };
        const name = typeof data.name === "string" ? data.name.slice(0, 200) : "";
        const raw = Array.isArray(data.ingredients) ? data.ingredients.slice(0, 100) : [];
        const lines = raw.filter((i) => typeof i === "string" && i.trim());
        let parsed = [];
        if (lines.length) {
            const { ok, data: body } = await send("/api/recipes/parse-ingredients", "POST", { lines });
            if (!ok) return { error: body.error || "Couldn't read the ingredients" };
            parsed = body.ingredients;
        }
        let next = 0;
        const ingredients = raw.map((i) => {
            if (typeof i === "string") return i.trim() ? parsed[next++] : null;
            if (i && typeof i === "object" && typeof i.name === "string") {
                return {
                    name: i.name,
                    qty: typeof i.qty === "number" || typeof i.qty === "string" ? i.qty : null,
                    unit: typeof i.unit === "string" ? i.unit : "",
                    note: typeof i.note === "string" ? i.note : "",
                };
            }
            return null;
        }).filter(Boolean);
        if (!name || !ingredients.length) return { error: 'JSON must include a "name" and at least one ingredient' };
        const steps = Array.isArray(data.steps)
            ? data.steps.filter((s) => typeof s === "string" && s.trim()).slice(0, 100).join("\n")
            : (typeof data.steps === "string" ? data.steps : "");
        const servingsValue = Number(data.servings);
        return {
            prefill: {
                name,
                notes: typeof data.notes === "string" ? data.notes.slice(0, 2000) : "",
                servings: Number.isInteger(servingsValue) && servingsValue >= 1 && servingsValue <= MAX_SERVINGS ? servingsValue : 4,
                source_url: typeof data.source_url === "string" ? data.source_url : "",
                ingredients,
                steps,
            },
        };
    }

    function openImportChooser() {
        const modal = openModal(importChooserHtml());
        const showImportError = (msg) => {
            const el = modal.querySelector("#import-error");
            el.textContent = msg;
            el.hidden = false;
        };
        modal.querySelector("#import-cancel").addEventListener("click", closeModal);

        const urlBtn = modal.querySelector("#import-url-btn");
        if (urlBtn) {
            urlBtn.addEventListener("click", async () => {
                const url = modal.querySelector("#import-url-input").value.trim();
                if (!url) return;
                const status = modal.querySelector("#import-url-status");
                status.hidden = false;
                status.textContent = "Fetching and extracting…";
                urlBtn.disabled = true;
                try {
                    const { ok, data } = await send("/api/recipes/extract-url", "POST", { url });
                    if (!ok) { showImportError(data.error || "Could not import that recipe"); return; }
                    openRecipeForm(null, {
                        name: data.name,
                        notes: data.notes,
                        servings: data.servings || 4,
                        source_url: data.source_url || url,
                        ingredients: data.ingredients || [],
                        steps: (data.steps || []).join("\n"),
                    });
                } finally {
                    urlBtn.disabled = false;
                    status.hidden = true;
                }
            });
        }

        async function useJsonText(text) {
            let parsed;
            try {
                parsed = JSON.parse(text);
            } catch (e) {
                showImportError("That is not valid JSON");
                return;
            }
            const result = await prefillFromImport(parsed);
            if (result.error) { showImportError(result.error); return; }
            openRecipeForm(null, result.prefill);
        }

        modal.querySelector("#import-json-btn").addEventListener("click", () => {
            const text = modal.querySelector("#import-json-input").value.trim();
            if (!text) { showImportError("Paste or choose a JSON file first"); return; }
            useJsonText(text);
        });

        modal.querySelector("#import-json-file").addEventListener("change", (e) => {
            const file = e.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = () => useJsonText(String(reader.result));
            reader.readAsText(file);
        });
    }

    // ---- SSE ----
    // Recipe edits from another tab or person show up here too.

    function connectSSE(onUpdate) {
        const es = new EventSource("/api/stream");
        es.addEventListener("update", onUpdate);
        es.onerror = () => { es.close(); setTimeout(() => connectSSE(onUpdate), 3000); };
    }

    // ---- Init ----

    if (recipeId) {
        detailView.hidden = false;
        loadDetail();
        // Don't yank the page out from under an open editor.
        connectSSE(() => { if (document.getElementById("modal-overlay").hidden) loadDetail(); });
    } else {
        libraryView.hidden = false;
        document.getElementById("new-recipe-btn").addEventListener("click", () => openRecipeForm(null));
        document.getElementById("import-recipe-btn").addEventListener("click", openImportChooser);
        searchInput.addEventListener("input", () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(loadRecipes, 200);
        });
        loadRecipes();
        connectSSE(loadRecipes);
    }
})();
