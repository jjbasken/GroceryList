/* global fetch */
// Shared helpers for the online-only pages (recipes, meals) and the shopping
// review sheet. The grocery list page keeps its own offline-aware api() in
// app.js; nothing here queues writes.
(function () {
    "use strict";

    let csrfToken = document.querySelector('meta[name="csrf-token"]').content;
    const accountId = document.querySelector('meta[name="account-id"]').content;

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

    // POST/PUT/DELETE a JSON body; resolves to { ok, data } and never throws on
    // HTTP errors, so callers can show data.error inline.
    async function send(url, method, body) {
        try {
            const res = await api(url, { method, body: body === undefined ? undefined : JSON.stringify(body) });
            const data = await res.json().catch(() => ({}));
            return { ok: res.ok, data };
        } catch (e) {
            return { ok: false, data: { error: "Couldn't reach the server — check your connection and try again." } };
        }
    }

    function escapeHtml(str) {
        // Used both as element text and inside quoted HTML attributes, so
        // quotes must be escaped too.
        return String(str == null ? "" : str)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    // ---- Local-calendar dates ----
    // Meals are planned on plain YYYY-MM-DD dates in the household's calendar.
    // They never pass through UTC, so "today" is always the phone's today.

    function toISODate(d) {
        const m = String(d.getMonth() + 1).padStart(2, "0");
        const day = String(d.getDate()).padStart(2, "0");
        return `${d.getFullYear()}-${m}-${day}`;
    }

    function parseISODate(s) {
        const [y, m, d] = s.split("-").map(Number);
        return new Date(y, m - 1, d);
    }

    function addDays(s, n) {
        const d = parseISODate(s);
        d.setDate(d.getDate() + n);
        return toISODate(d);
    }

    // Plans run Monday to Sunday.
    function weekStart(s) {
        const dow = parseISODate(s).getDay();
        return addDays(s, dow === 0 ? -6 : 1 - dow);
    }

    function todayISO() { return toISODate(new Date()); }

    function dayLabel(s, opts) {
        return parseISODate(s).toLocaleDateString(undefined, opts || { weekday: "short", month: "short", day: "numeric" });
    }

    // ---- Modal ----
    // One overlay per page, created on demand, so pages without markup for it
    // (the grocery list) can still open the shopping review.

    let overlay = null;
    let modal = null;
    let onClose = null;

    function ensureModal() {
        if (overlay) return;
        overlay = document.getElementById("modal-overlay");
        modal = document.getElementById("modal");
        if (!overlay) {
            overlay = document.createElement("div");
            overlay.id = "modal-overlay";
            overlay.className = "modal-overlay";
            overlay.hidden = true;
            modal = document.createElement("div");
            modal.id = "modal";
            modal.className = "modal";
            modal.setAttribute("role", "dialog");
            modal.setAttribute("aria-modal", "true");
            overlay.appendChild(modal);
            document.body.appendChild(overlay);
        }
        overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
        document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !overlay.hidden) closeModal(); });
    }

    function openModal(html, closeCallback) {
        ensureModal();
        modal.innerHTML = html;
        overlay.hidden = false;
        onClose = closeCallback || null;
        const focusable = modal.querySelector("input, select, textarea, button");
        if (focusable) focusable.focus({ preventScroll: true });
        return modal;
    }

    function closeModal() {
        if (!overlay) return;
        overlay.hidden = true;
        modal.innerHTML = "";
        const cb = onClose;
        onClose = null;
        if (cb) cb();
    }

    // ---- Toast ----

    let toastTimer = null;
    function toast(message) {
        let el = document.getElementById("toast");
        if (!el) {
            el = document.createElement("div");
            el.id = "toast";
            el.className = "toast";
            el.setAttribute("role", "status");
            document.body.appendChild(el);
        }
        el.textContent = message;
        el.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
    }

    window.PageApi = {
        api, send, escapeHtml, openModal, closeModal, toast,
        dates: { toISODate, parseISODate, addDays, weekStart, todayISO, dayLabel },
    };
})();
