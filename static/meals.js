/* global PageApi, ShoppingReview, EventSource */
// Meal planning: a Monday-to-Sunday week of planned meals.
//
// Kinds follow MenuPlanner: a "cook" night (usually a recipe; the only kind
// whose ingredients go to the list), "leftovers" from an earlier cook night,
// and a "flexible" easy night. Online-only, like the recipes page.
//
// Meals can also go on the week's menu without a day (unscheduled) and be
// picked for a day later. Kids' lunches (home or school) sit on each weekday.
(function () {
    "use strict";

    const { api, send, escapeHtml: esc, openModal, closeModal, toast } = PageApi;
    const { addDays, weekStart, todayISO, dayLabel } = PageApi.dates;

    const KIND_LABELS = { cook: "Cook", leftovers: "Leftovers", flexible: "Flexible" };
    const LUNCH_LABELS = { home: "Home", school: "School" };
    const SCHOOL_DAYS = 5; // Monday to Friday

    const daysEl = document.getElementById("week-days");
    const errorEl = document.getElementById("meals-error");
    const addWeekBtn = document.getElementById("add-week-btn");
    const thisWeekBtn = document.getElementById("this-week-btn");
    const menuEl = document.getElementById("menu-meals");
    const copyLunchesBtn = document.getElementById("copy-lunches-btn");

    let today = todayISO();
    let week = initialWeek();
    let meals = [];
    let lunches = [];
    let kids = [];
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
            const [mealRes, lunchRes, kidRes] = await Promise.all([
                api(`/api/meals?start=${week}&days=7`),
                api(`/api/lunches?start=${week}&days=7`),
                api("/api/kids"),
            ]);
            if (!mealRes.ok || !lunchRes.ok || !kidRes.ok) throw new Error("load_failed");
            const [mealData, lunchData, kidData] = await Promise.all([mealRes.json(), lunchRes.json(), kidRes.json()]);
            if (seq !== loadSeq) return; // a newer week was requested meanwhile
            meals = mealData;
            lunches = lunchData;
            kids = kidData;
            showError("");
            render();
        } catch (e) {
            if (seq !== loadSeq || e.message === "unauthorized") return;
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
        // Clear the previous week before offering menu and shopping actions.
        meals = [];
        lunches = [];
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

    function scheduled() { return meals.filter((m) => !m.unscheduled); }
    function menuMeals() { return meals.filter((m) => m.unscheduled); }

    function dayOptions(placeholder) {
        let html = `<option value="">${esc(placeholder)}</option>`;
        for (let i = 0; i < 7; i++) {
            const date = addDays(week, i);
            html += `<option value="${date}">${esc(dayLabel(date, { weekday: "long" }))}</option>`;
        }
        return html;
    }

    function mealHtml(meal, cookNo) {
        const kindText = meal.kind === "cook" ? `Cook ${cookNo.get(meal.id) || ""}` : KIND_LABELS[meal.kind];
        const servings = meal.servings || (meal.recipe_id ? meal.recipe_servings : null);
        const meta = [];
        if (servings) meta.push(`Serves ${servings}`);
        if (meal.kind === "cook" && !meal.recipe_id) meta.push("No recipe");
        if (meal.added_to_list_at) meta.push(`✓ On ${esc(meal.added_to_list_name || "a list")}`);
        const picker = meal.unscheduled ? `
                <select class="pick-day" data-meal-id="${meal.id}" aria-label="Pick a day for ${esc(meal.title)}">
                    ${dayOptions("Pick a day…")}
                </select>` : "";
        return `
            <li class="${meal.unscheduled ? "menu-item" : ""}">
                <button type="button" class="meal-card kind-${meal.kind}" data-meal-id="${meal.id}">
                    <span class="kind-pill pill-${meal.kind}">${esc(kindText.trim())}</span>
                    <span class="meal-title">${esc(meal.title)}</span>
                    ${meal.notes ? `<span class="meal-notes">${esc(meal.notes)}</span>` : ""}
                    ${meta.length ? `<span class="meal-meta">${meta.join(" · ")}</span>` : ""}
                </button>${picker}
            </li>`;
    }

    function lunchFor(kidId, date) {
        return lunches.find((l) => l.kid_id === kidId && l.date === date) || null;
    }

    function lunchesHtml(date) {
        if (!kids.length) return "";
        const rows = kids.map((kid) => {
            const lunch = lunchFor(kid.id, date);
            const what = lunch
                ? `<span class="lunch-pill lunch-${lunch.kind}">${LUNCH_LABELS[lunch.kind]}</span>${lunch.choice ? `<span class="lunch-choice">${esc(lunch.choice)}</span>` : ""}`
                : '<span class="lunch-unset">Not set</span>';
            return `
                <li>
                    <button type="button" class="lunch-row" data-kid-id="${kid.id}" data-date="${date}"
                            aria-label="${esc(kid.name)}'s lunch on ${esc(dayLabel(date, { weekday: "long" }))}: ${lunch ? LUNCH_LABELS[lunch.kind] : "Not set"}${lunch && lunch.choice ? `, ${esc(lunch.choice)}` : ""}">
                        <span class="lunch-kid">${esc(kid.name)}</span>${what}
                    </button>
                </li>`;
        }).join("");
        return `<div class="day-lunches"><span class="day-lunches-label">Lunch</span><ul>${rows}</ul></div>`;
    }

    function renderLunchSummary() {
        const summary = document.getElementById("lunch-summary");
        document.getElementById("no-kids-hint").hidden = kids.length > 0;
        copyLunchesBtn.hidden = kids.length === 0;
        const kidIds = new Set(kids.map((k) => k.id));
        const counts = { home: 0, school: 0 };
        lunches.filter((l) => kidIds.has(l.kid_id)).forEach((l) => { counts[l.kind] += 1; });
        summary.textContent = kids.length && (counts.home || counts.school)
            ? `${counts.home} to pack · ${counts.school} at school` : "";
    }

    function render() {
        document.getElementById("week-label").textContent = weekName();
        document.getElementById("week-range").textContent =
            `${dayLabel(week, { month: "short", day: "numeric" })} – ${dayLabel(addDays(week, 6), { month: "short", day: "numeric" })}`;
        thisWeekBtn.hidden = week === weekStart(today);

        const cookNo = cookNumbers();
        menuEl.innerHTML = menuMeals().map((m) => mealHtml(m, cookNo)).join("");
        renderLunchSummary();

        const html = [];
        for (let i = 0; i < 7; i++) {
            const date = addDays(week, i);
            const dayMeals = scheduled().filter((m) => m.date === date);
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
                    ${i < SCHOOL_DAYS ? lunchesHtml(date) : ""}
                </li>`);
        }
        daysEl.innerHTML = html.join("");
        document.querySelectorAll(".meals-main .meal-card").forEach((btn) => {
            btn.addEventListener("click", () => {
                const meal = meals.find((m) => m.id === Number(btn.dataset.mealId));
                if (meal) openMealSheet(meal.date, meal);
            });
        });
        menuEl.querySelectorAll(".pick-day").forEach((select) => {
            select.addEventListener("change", () => {
                const meal = meals.find((m) => m.id === Number(select.dataset.mealId));
                if (meal && select.value) scheduleMeal(meal, select.value);
            });
        });
        daysEl.querySelectorAll(".add-meal-btn").forEach((btn) => {
            btn.addEventListener("click", () => openMealSheet(btn.dataset.date, null));
        });
        daysEl.querySelectorAll(".lunch-row").forEach((btn) => {
            btn.addEventListener("click", () => {
                const kid = kids.find((k) => k.id === Number(btn.dataset.kidId));
                if (kid) openLunchSheet(kid, btn.dataset.date);
            });
        });

        const cookWithRecipe = meals.filter((m) => m.kind === "cook" && m.recipe_id);
        addWeekBtn.disabled = cookWithRecipe.length === 0;
        addWeekBtn.title = addWeekBtn.disabled ? "Plan a cook night with a recipe first" : "";
    }

    // ---- Week menu ----

    function mealPayload(meal, changes) {
        return {
            date: meal.date, unscheduled: !!meal.unscheduled, kind: meal.kind, title: meal.title,
            recipe_id: meal.recipe_id, servings: meal.servings, leftover_of: meal.leftover_of,
            notes: meal.notes || "", ...changes,
        };
    }

    async function scheduleMeal(meal, date) {
        const { ok, data } = await send(`/api/meals/${meal.id}`, "PUT", mealPayload(meal, { date, unscheduled: false }));
        if (!ok) { showError(data.error || "Couldn't move the meal."); loadWeek(); return; }
        showError("");
        toast(`${meal.title} → ${dayLabel(date, { weekday: "long" })}`);
        loadWeek();
    }

    // ---- Lunch sheet ----

    function openLunchSheet(kid, date) {
        const lunch = lunchFor(kid.id, date);
        let kind = lunch ? lunch.kind : "";
        const options = { ...LUNCH_LABELS, "": "Not set" };
        const modal = openModal(`
            <h2>${esc(kid.name)} · ${esc(dayLabel(date, { weekday: "long", month: "short", day: "numeric" }))}</h2>
            <form id="lunch-form" novalidate>
                <div class="kind-switch" role="radiogroup" aria-label="Lunch">
                    ${Object.entries(options).map(([value, label]) => `
                        <label class="kind-option">
                            <input type="radio" name="lunch-kind" value="${value}" ${value === kind ? "checked" : ""}>
                            <span>${label}</span>
                        </label>`).join("")}
                </div>
                <label id="choice-field">What are they having?
                    <input type="text" id="lunch-choice" maxlength="200" autocomplete="off"
                           value="${esc(lunch && lunch.choice ? lunch.choice : "")}">
                </label>
                <p id="lunch-error" class="modal-error" hidden></p>
                <div class="modal-actions">
                    <button type="button" id="lunch-cancel" class="admin-action-btn">Cancel</button>
                    <button type="submit" class="admin-action-btn btn-ok">Save</button>
                </div>
            </form>
        `);
        const choiceInput = modal.querySelector("#lunch-choice");
        const errorBox = modal.querySelector("#lunch-error");

        function applyKind() {
            modal.querySelector("#choice-field").hidden = !kind;
            choiceInput.placeholder = kind === "school" ? "e.g. Pizza from the school menu" : "e.g. Turkey sandwich, apple";
        }

        modal.querySelectorAll('input[name="lunch-kind"]').forEach((radio) => {
            radio.addEventListener("change", () => { kind = radio.value; applyKind(); if (kind) choiceInput.focus(); });
        });
        modal.querySelector("#lunch-cancel").addEventListener("click", closeModal);
        modal.querySelector("#lunch-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            const payload = { kid_id: kid.id, date, kind: kind || null, choice: kind ? choiceInput.value.trim() : null };
            const { ok, data } = await send("/api/lunches", "PUT", payload);
            if (!ok) { errorBox.textContent = data.error || "Couldn't save the lunch."; errorBox.hidden = false; return; }
            closeModal();
            loadWeek();
        });
        applyKind();
    }

    // ---- Kids sheet ----

    function openKidsSheet() {
        // Live page refreshes must not replace the sheet's working state.
        let sheetKids = kids.map((kid) => ({ ...kid }));
        let adding = false;
        const modal = openModal(`
            <h2>Kids</h2>
            <p class="modal-hint">Each kid gets a home or school lunch on every school day.</p>
            <ul id="kids-list" class="kids-list"></ul>
            <form id="kid-form" class="kid-add" novalidate>
                <input type="text" id="kid-name" maxlength="50" autocomplete="off" placeholder="Add a kid" aria-label="Kid's name">
                <button type="submit" class="admin-action-btn btn-ok">Add</button>
            </form>
            <p id="kids-error" class="modal-error" hidden></p>
            <div class="modal-actions">
                <button type="button" id="kids-done" class="admin-action-btn">Done</button>
            </div>
        `, loadWeek);
        const listEl = modal.querySelector("#kids-list");
        const errorBox = modal.querySelector("#kids-error");
        const nameInput = modal.querySelector("#kid-name");

        function fail(data, fallback) {
            errorBox.textContent = data.error || fallback;
            errorBox.hidden = false;
        }

        function renderKids() {
            listEl.innerHTML = sheetKids.map((kid) => `
                <li class="kid-row">
                    <input type="text" maxlength="50" value="${esc(kid.name)}" data-kid-id="${kid.id}" aria-label="Name">
                    <button type="button" class="admin-action-btn btn-warn" data-remove="${kid.id}">Remove</button>
                </li>`).join("");
            listEl.querySelectorAll("input").forEach((input) => {
                input.addEventListener("change", async () => {
                    const { ok, data } = await send(`/api/kids/${input.dataset.kidId}`, "PUT", { name: input.value });
                    if (!ok) { fail(data, "Couldn't rename."); return; }
                    errorBox.hidden = true;
                    const kid = sheetKids.find((k) => k.id === data.id);
                    if (kid) kid.name = data.name;
                });
            });
            listEl.querySelectorAll("[data-remove]").forEach((btn) => {
                btn.addEventListener("click", async () => {
                    const kid = sheetKids.find((k) => k.id === Number(btn.dataset.remove));
                    if (!kid || !window.confirm(`Remove ${kid.name} and all their lunches?`)) return;
                    const { ok, data } = await send(`/api/kids/${kid.id}`, "DELETE");
                    if (!ok) { fail(data, "Couldn't remove."); return; }
                    sheetKids = sheetKids.filter((k) => k.id !== kid.id);
                    renderKids();
                });
            });
        }

        modal.querySelector("#kid-form").addEventListener("submit", async (e) => {
            e.preventDefault();
            if (adding) return;
            adding = true;
            const submit = modal.querySelector('#kid-form button[type="submit"]');
            submit.disabled = true;
            const { ok, data } = await send("/api/kids", "POST", { name: nameInput.value });
            adding = false;
            submit.disabled = false;
            if (!ok) { fail(data, "Couldn't add."); return; }
            errorBox.hidden = true;
            sheetKids.push(data);
            nameInput.value = "";
            renderKids();
            nameInput.focus();
        });
        modal.querySelector("#kids-done").addEventListener("click", closeModal);
        renderKids();
        if (!sheetKids.length) nameInput.focus();
    }

    // ---- Add / edit sheet ----

    function recipeMatches(query) {
        const q = query.trim().toLowerCase();
        const list = q ? recipes.filter((r) => r.name.toLowerCase().includes(q)) : recipes;
        return list.slice(0, 8);
    }

    // date is null when adding to the week's menu.
    function openMealSheet(date, meal) {
        const editing = !!meal;
        let kind = meal ? meal.kind : "cook";
        let recipeId = meal ? meal.recipe_id : null;
        const onMenu = editing ? !!meal.unscheduled : date === null;
        const heading = editing ? "Edit meal"
            : onMenu ? "Add to this week's menu" : `Add a meal · ${esc(dayLabel(date, { weekday: "long" }))}`;
        // Adding to a day: offer this week's menu first, so picking is one tap.
        const pickable = !editing && !onMenu ? menuMeals() : [];

        const modal = openModal(`
            <h2>${heading}</h2>
            ${pickable.length ? `
                <div class="menu-pick">
                    <p class="modal-hint">Pick from this week's menu</p>
                    <div class="menu-pick-options">
                        ${pickable.map((m) => `<button type="button" class="menu-pick-btn" data-meal-id="${m.id}">${esc(m.title)}</button>`).join("")}
                    </div>
                    <p class="modal-hint">…or add something new:</p>
                </div>` : ""}
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
                    <label id="date-field">Date
                        <input type="date" id="meal-date" value="${date || week}" required>
                    </label>
                </div>
                <label class="check-label" id="menu-field">
                    <input type="checkbox" id="meal-unscheduled" ${onMenu ? "checked" : ""}>
                    <span>No set day: keep it on the week's menu</span>
                </label>
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
        const unscheduledBox = modal.querySelector("#meal-unscheduled");

        function applyUnscheduled() {
            modal.querySelector("#date-field").hidden = unscheduledBox.checked && kind !== "leftovers";
        }
        unscheduledBox.addEventListener("change", applyUnscheduled);

        modal.querySelectorAll(".menu-pick-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                const picked = meals.find((m) => m.id === Number(btn.dataset.mealId));
                closeModal();
                if (picked) scheduleMeal(picked, date);
            });
        });

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
            const cooks = scheduled().filter((m) => m.kind === "cook" && m.date <= mealDate && (!meal || m.id !== meal.id));
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
            modal.querySelector("#menu-field").hidden = kind === "leftovers";
            applyUnscheduled();
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
                unscheduled: kind !== "leftovers" && unscheduledBox.checked,
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
    document.getElementById("add-menu-btn").addEventListener("click", () => openMealSheet(null, null));
    document.getElementById("manage-kids-btn").addEventListener("click", openKidsSheet);
    copyLunchesBtn.addEventListener("click", async () => {
        const { ok, data } = await send("/api/lunches/copy-week", "POST", { from: addDays(week, -7), to: week });
        if (!ok) { showError(data.error || "Couldn't copy lunches."); return; }
        toast(data.copied ? `Copied ${data.copied} lunch${data.copied === 1 ? "" : "es"} from last week`
            : "Nothing new to copy from last week");
        loadWeek();
    });
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
