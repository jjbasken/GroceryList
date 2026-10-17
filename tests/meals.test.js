// Run with: node tests/meals.test.js
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const tick = () => new Promise(resolve => setImmediate(resolve));

function page() {
    const nodes = new Map();
    function node(id) {
        if (!nodes.has(id)) nodes.set(id, {
            innerHTML: '', value: '', handlers: {},
            addEventListener(event, fn) { this.handlers[event] = fn; },
            querySelector: node, querySelectorAll: () => [], focus() {},
        });
        return nodes.get(id);
    }
    const stream = {};
    let menu = [{ id: 10, date: '2026-10-12', title: 'Tacos', kind: 'cook', unscheduled: 1 }];
    let kids = [];
    let pending = false;
    let addCount = 0;
    let finishAdd;
    const context = vm.createContext({
        URLSearchParams,
        window: { location: { search: '' } }, history: { replaceState() {} },
        document: { getElementById: node, querySelectorAll: () => [], addEventListener() {} },
        EventSource: function () { this.addEventListener = (event, fn) => { stream[event] = fn; }; },
        PageApi: {
            escapeHtml: String, toast() {}, closeModal() {},
            openModal: () => node('modal'),
            dates: {
                todayISO: () => '2026-10-12', weekStart: s => s,
                dayLabel: s => s,
                addDays(s, n) {
                    const date = new Date(`${s}T12:00:00Z`);
                    date.setUTCDate(date.getUTCDate() + n);
                    return date.toISOString().slice(0, 10);
                },
            },
            async api(url) {
                if (pending) return new Promise(() => {});
                return { ok: true, json: async () => url.startsWith('/api/meals?') ? menu
                    : url === '/api/kids' ? kids.map(k => ({ ...k })) : [] };
            },
            async send() {
                addCount++;
                return new Promise(resolve => { finishAdd = resolve; });
            },
        },
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../static/meals.js'), 'utf8'), context);
    return {
        node, stream,
        holdLoads() { pending = true; },
        setKids(value) { kids = value; },
        finishAdd(data) { finishAdd({ ok: true, data }); },
        get addCount() { return addCount; },
    };
}

test('switching weeks immediately removes stale menu and shopping actions', async () => {
    const p = page();
    await tick();
    assert.match(p.node('menu-meals').innerHTML, /Tacos/);
    p.holdLoads();
    p.node('next-week').handlers.click();
    assert.equal(p.node('menu-meals').innerHTML, '');
    assert.equal(p.node('add-week-btn').disabled, true);
});

test('live refresh during kid creation cannot duplicate the child in the sheet', async () => {
    const p = page();
    await tick();
    p.node('manage-kids-btn').handlers.click();
    p.node('#kid-name').value = 'Ava';
    const request = p.node('#kid-form').handlers.submit({ preventDefault() {} });
    // The server broadcasts before its create response reaches the client.
    p.setKids([{ id: 1, name: 'Ava' }]);
    p.stream.update();
    await tick();
    p.finishAdd({ id: 1, name: 'Ava' });
    await request;
    assert.equal((p.node('#kids-list').innerHTML.match(/data-remove="1"/g) || []).length, 1);
});

test('repeated submit while adding a child sends only one write', async () => {
    const p = page();
    await tick();
    p.node('manage-kids-btn').handlers.click();
    p.node('#kid-name').value = 'Ava';
    const submit = () => p.node('#kid-form').handlers.submit({ preventDefault() {} });
    const first = submit();
    await submit();
    assert.equal(p.addCount, 1);
    p.finishAdd({ id: 1, name: 'Ava' });
    await first;
});
