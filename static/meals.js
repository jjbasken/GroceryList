/* global PageApi, ShoppingReview, EventSource */
// Meal planning: a Monday-to-Sunday week of planned meals.
//
// Kinds follow MenuPlanner: a "cook" night (usually a recipe; the only kind
// whose ingredients go to the list), "leftovers" from an earlier cook night,
// and a "flexible" easy night. Online-only, like the recipes page.
(function () {
    "use strict";

    const { api, send, escapeHtml: esc, openModal, closeModal, toast } = PageApi;
    const { addDays, weekStart, todayISO, dayLabel } = PageApi.dates;

    const KIND_LABELS = { cook: "Cook", leftovers: "Leftovers", flexible: "Flexible" };

    const daysEl = document.getElementById("week-days");
    const errorEl = document.getElementById("meals-error");
    const addWeekBtn = document.getElementById("add-week-btn");
    const thisWeekBtn = document.getElementById("this-week-btn");

    let today = todayISO();
    let week = initialWeek();
    let meals = [];
    let recipes = [];
    let loadSeq = 0;

    function initialWeek() {
        const param = new URLSearchParams(window.location.search).get("week");
        return param && /^\d{4}-\d{2}-\d{2}$/.test(param) ? weekStart(param) : weekStart(today);
    }

    function showError(msg) {
        errorEl.textContent = msg || "";
        errorEl.hidden = !msg;
    }

    // ---- Loading ----

    async function loadWeek() {
        const seq = ++loadSeq;
        try {
            const res = await api(`/api/meals?start=${week}&days=7`);
            if (!res.ok) throw new Error("load_failed");
            const data = await res.json();
            if (seq !== loadSeq) return; // a newer week was requested meanwhile
            meals = data;
            showError("");
            render();
        } catch (e) {
            if (e.message === "unauthorized") return;
            showError("Couldn't load meals — check your connection.");
        }
    }

    async function loadRecipes() {
        try {
            const res = await api("/api/recipes");
            if (res.ok) recipes = await res.json();
        } catch (e) { /* the picker falls back to free text */ }
    }

    function goToWeek(start) {
        week = start;
        const url = start === weekStart(today) ? "/meals" : `/meals?week=${start}`;
        history.replaceState(null, "", url);
        render();
        loadWeek();
    }

    // ---- Rendering ----

    function weekName() {
        const current = weekStart(today);
        if (week === current) return "This week";
        if (week === addDays(current, 7)) return "Next week";
        if (week === addDays(current, -7)) return "Last week";
        return `Week of ${dayLabel(week, { month: "short", day: "numeric" })}`;
    }

    function cookNumbers() {
        const cooks = meals.filter((m) => m.kind === "cook");
        return new Map(cooks.map((m, i) => [m.id, `${i + 1} of ${cooks.length}`]));
    }

    function mealHtml(meal, cookNo) {
        const kindText = meal.kind === "cook" ? `Cook ${cookNo.get(meal.id) || ""}` : KIND_LABELS[meal.kind];
        const servings = meal.servings || (meal.recipe_id ? meal.recipe_servings : null);
        const meta = [];
        if (servings) meta.push(`Serves ${servings}`);
        if (meal.kind === "cook" && !meal.recipe_id) meta.push("No recipe");
        if (meal.added_to_list_at) meta.push(`✓ On ${esc(meal.added_to_list_name || "a list")}`);
        return `
            <li>
                <button type="button" class="meal-card kind-${meal.kind}" data-meal-id="${meal.id}">
                    <span class="kind-pill pill-${meal.kind}">${esc(kindText.trim())}</span>
                    <span class="meal-title">${esc(meal.title)}</span>
                    ${meal.notes ? `<span class="meal-notes">${esc(meal.notes)}</span>` : ""}
                    ${meta.length ? `<span class="meal-meta">${meta.join(" · ")}</span>` : ""}
                </button>
            </li>`;
    }

    function render() {
        document.getElementById("week-label").textContent = weekName();
        document.getElementById("week-range").textContent =
            `${dayLabel(week, { month: "short", day: "numeric" })} – ${dayLabel(addDays(week, 6), { month: "short", day: "numeric" })}`;
        thisWeekBtn.hidden = week === weekStart(today);

        const cookNo = cookNumbers();
        const html = [];
        for (let i = 0; i < 7; i++) {
            const date = addDays(week, i);
            const dayMeals = meals.filter((m) => m.date === date);
            const isToday = date === today;
            html.push(`
                <li class="week-day${isToday ? " is-today" : ""}${date < today ? " is-past" : ""}">
                    <div class="day-head">
                        <span class="day-name">${esc(dayLabel(date, { weekday: "short" }))}</span>
                        <span class="day-date">${esc(dayLabel(date, { month: "short", day: "numeric" }))}</span>
                        ${isToday ? '<span class="today-tag">Today</span>' : ""}
                    </div>
                    <ul class="day-meals">${dayMeals.map((m) => mealHtml(m, cookNo)).join("")}</ul>
                    <button type="button" class="add-meal-btn" data-date="${date}" aria-label="Add a meal on ${esc(dayLabel(date, { weekday: "long" }))}">+ ${dayMeals.length ? "Add another" : "Add meal"}</button>
                </li>`);
        }
        daysEl.innerHTML = html.join("");
        daysEl.querySelectorAll(".meal-card").forEach((btn) => {
            btn.addEventListener("click", () => {
                const meal = meals.find((m) => m.id === Number(btn.dataset.mealId));
                if (meal) openMealSheet(meal.date, meal);
            });
        });
        daysEl.querySelectorAll(".add-meal-btn").forEach((btn) => {
            btn.addEventListener("click", () => openMealSheet(btn.dataset.date, null));
        });

        const cookWithRecipe = meals.filter((m) => m.kind === "cook" && m.recipe_id);
        addWeekBtn.disabled = cookWithRecipe.length === 0;
        addWeekBtn.title = addWeekBtn.disabled ? "Plan a cook night with a recipe first" : "";
    }

    // ---- Add / edit sheet ----

    function recipeMatches(query) {
        const q = query.trim().toLowerCase();
        const list = q ? recipes.filter((r) => r.name.toLowerCase().includes(q)) : recipes;
        return list.slice(0, 8);
    }

    function openMealSheet(date, meal) {
        const editing = !!meal;
        let kind = meal ? meal.kind : "cook";
        let recipeId = meal ? meal.recipe_id : null;

        const modal = openModal(`
            <h2>${editing ? "Edit meal" : `Add a meal · ${esc(dayLabel(date, { weekday: "long" }))}`}</h2>
            <form id="meal-form" novalidate>
                <div class="kind-switch" role="radiogroup" aria-label="Kind of night">
                    ${Object.entries(KIND_LABELS).map(([value, label]) => `
                        <label class="kind-option">
                            <input type="radio" name="meal-kind" value="${value}" ${value === kind ? "checked" : ""}>
                            <span>${label}</span>
                        </label>`).join("")}
                </div>
                <p id="kind-hint" class="modal-hint"></p>
                <div id="leftover-field" hidden>
                    <label>From which cook night?
                        <select id="meal-leftover"></select>
                    </label>
                </div>
                <div id="title-field" class="recipe-picker">
                    <label><span id="title-label">Recipe or dish</span>
                        <input type="text" id="meal-title" maxlength="200" autocomplete="off"
                               value="${esc(meal ? meal.title : "")}" placeholder="Search recipes or type a dish">
                    </label>
                    <div id="recipe-suggestions" class="picker-suggestions" hidden></div>
                    <p id="recipe-linked" class="picker-linked" hidden></p>
                </div>
                <div class="meal-row">
                    <label id="servings-field">Servings
                        <input type="number" id="meal-servings" min="1" max="100" inputmode="numeric"
                               value="${meal && meal.servings ? meal.servings : ""}">
                    </label>
                    <label>Date
                        <input type="date" id="meal-date" value="${date}" required>
                    </label>
                </div>
                <label>Notes
                    <input type="text" id="meal-notes" maxlength="500" value="${esc(meal && meal.notes ? meal.notes : "")}"
                           placeholder="e.g. eggs for breakfast too">
                </label>
                <p id="meal-error" class="modal-error" hidden></p>
                <div class="modal-actions">
                    ${editing ? '<button type="button" id="meal-delete" class="admin-action-btn btn-warn">Remove</button>' : ""}
                    <button type="button" id="meal-cancel" class="admin-action-btn">Cancel</button>
                    <button type="submit" class="admin-action-btn btn-ok">${editing ? "Save" : "Add"}</button>
                </div>
            </form>
        `);

        const titleInput = modal.querySelector("#meal-title");
        const servingsInput = modal.querySelector("#meal-servings");
        const suggestions = modal.querySelector("#recipe-suggestions");
        const linked = modal.querySelector("#recipe-linked");
        const leftoverSelect = modal.querySelector("#meal-leftover");
        const errorBox = modal.querySelector("#meal-error");

        function linkedRecipe() {
            return recipes.find((r) => r.id === recipeId) || null;
        }

        function updateLinked() {
            const recipe = linkedRecipe();
            linked.hidden = !(kind === "cook" && recipeId);
            linked.textContent = recipe ? `Recipe: ${recipe.name} (serves ${recipe.servings})`
                : recipeId ? `Recipe: ${meal && meal.recipe_name ? meal.recipe_name : "linked"}` : "";
            servingsInput.placeholder = recipe ? String(recipe.servings) : "";
        }

        function fillLeftovers() {
            const mealDate = modal.querySelector("#meal-date").value;
            const cooks = meals.filter((m) => m.kind === "cook" && m.date <= mealDate && (!meal || m.id !== meal.id));
            const current = meal && meal.leftover_of;
            leftoverSelect.innerHTML = '<option value="">Something else</option>' + cooks.map((m) =>
                `<option value="${m.id}" ${m.id === current ? "selected" : ""}>${esc(dayLabel(m.date, { weekday: "short" }))} · ${esc(m.title)}</option>`).join("");
        }

        function applyKind() {
            const hints = {
                cook: "Pick a recipe so its ingredients can go on the list, or just type a dish.",
                leftovers: "Eating up an earlier cook night. The name fills in for you.",
                flexible: "A loose, easy night — no ingredients are added.",
            };
            modal.querySelector("#kind-hint").textContent = hints[kind];
            modal.querySelector("#leftover-field").hidden = kind !== "leftovers";
            modal.querySelector("#servings-field").hidden = kind !== "cook";
            modal.querySelector("#title-label").textContent = kind === "cook" ? "Recipe or dish" : "What's for dinner";
            titleInput.placeholder = kind === "cook" ? "Search recipes or type a dish"
                : kind === "leftovers" ? "Leave blank to use the cook night's name" : "e.g. Frittata, omelets or leftovers";
            if (kind === "leftovers") fillLeftovers();
            suggestions.hidden = true;
            updateLinked();
        }

        function showSuggestions() {
            if (kind !== "cook") { suggestions.hidden = true; return; }
            const matches = recipeMatches(titleInput.value);
            if (!matches.length) { suggestions.hidden = true; return; }
            suggestions.innerHTML = matches.map((r) =>
                `<button type="button" class="picker-option" data-id="${r.id}">${esc(r.name)}<span class="picker-meta">Serves ${r.servings}</span></button>`).join("");
            suggestions.hidden = false;
            suggestions.querySelectorAll(".picker-option").forEach((btn) => {
                // mousedown fires before the input's blur hides the list
                btn.addEventListener("mousedown", (e) => e.preventDefault());
                btn.addEventListener("click", () => {
                    const recipe = recipes.find((r) => r.id === Number(btn.dataset.id));
                    recipeId = recipe.id;
                    titleInput.value = recipe.name;
                    suggestions.hidden = true;
                    updateLinked();
                });
            });
        }

        modal.querySelectorAll('input[name="meal-kind"]').forEach((radio) => {
            radio.addEventListener("change", () => { kind = radio.value; applyKind(); });
        });
        titleInput.addEventListener("input", () => {
            // Typing over a picked recipe turns the meal back into free text.
            const recipe = linkedRecipe();
            if (recipeId && (!recipe || titleInput.value !== recipe.name)) recipeId = null;
            updateLinked();
            showSuggestions();
        });
        titleInput.addEventListener("focus", showSuggestions);
        titleInput.addEventListener("blur", () => { suggestions.hidden = true; });
        modal.querySelector("#meal-date").addEventListener("change", () => { if (kind === "leftovers") fillLeftovers(); });
        modal.querySelector("#meal-cancel").addEventListener("click", closeModal);

        const deleteBtn = modal.querySelector("#meal-delete");
        if (deleteBtn) {
            deleteBtn.addEventListener("click", async () => {
                const { ok, data } = await send(`/api/meals/${meal.id}`, "DELETE");
                if (!ok) { errorBox.textContent = data.error || "Couldn't remove the meal."; errorBox.hidden = false; return; }
                closeModal();
                loadWeek();
            });
        }

        modal.querySelector("#meal-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            errorBox.hidden = true;
            const servings = servingsInput.value.trim();
            const leftoverOf = leftoverSelect.value;
            const payload = {
                date: modal.querySelector("#meal-date").value,
                kind,
                title: titleInput.value.trim(),
                recipe_id: kind === "cook" ? recipeId : null,
                servings: kind === "cook" && servings ? Number(servings) : null,
                leftover_of: kind === "leftovers" && leftoverOf ? Number(leftoverOf) : null,
                notes: modal.querySelector("#meal-notes").value.trim(),
            };
            const { ok, data } = editing
                ? await send(`/api/meals/${meal.id}`, "PUT", payload)
                : await send("/api/meals", "POST", payload);
            if (!ok) { errorBox.textContent = data.error || "Couldn't save the meal."; errorBox.hidden = false; return; }
            closeModal();
            if (data.date && (data.date < week || data.date > addDays(week, 6))) {
                toast(`Moved to ${dayLabel(data.date)}`);
            }
            loadWeek();
        });

        applyKind();
    }

    // ---- Events ----

    document.getElementById("prev-week").addEventListener("click", () => goToWeek(addDays(week, -7)));
    document.getElementById("next-week").addEventListener("click", () => goToWeek(addDays(week, 7)));
    thisWeekBtn.addEventListener("click", () => goToWeek(weekStart(today)));
    addWeekBtn.addEventListener("click", () => {
        ShoppingReview.open({
            mealIds: meals.map((m) => m.id),
            title: `${weekName()} · ${dayLabel(week, { month: "short", day: "numeric" })} – ${dayLabel(addDays(week, 6), { month: "short", day: "numeric" })}`,
            onDone: loadWeek,
        });
    });

    // "Today" follows the phone's calendar, including across midnight.
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && todayISO() !== today) {
            today = todayISO();
            render();
        }
    });

    function connectSSE() {
        const es = new EventSource("/api/stream");
        es.addEventListener("update", () => { loadWeek(); loadRecipes(); });
        es.onerror = () => { es.close(); setTimeout(connectSSE, 3000); };
    }

    // ---- Init ----
    render();
    loadWeek();
    loadRecipes();
    connectSSE();
})();
