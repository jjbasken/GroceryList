// Run with: node tests/offline-security.test.js
// Execute the real browser scripts against deterministic browser API doubles.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname, '../static', name), 'utf8');
const origin = 'https://grocery.test';

function indexedDBDouble(entries = []) {
    const rows = new Map(entries.map((value, key) => [key + 1, value]));
    const database = {
        close() {},
        transaction() {
            const tx = { objectStore: () => ({
                clear: () => rows.clear(),
                delete: key => rows.delete(key),
                add: value => rows.set(Math.max(0, ...rows.keys()) + 1, value),
                getAll: () => result([...rows.values()]),
                getAllKeys: () => result([...rows.keys()]),
            }) };
            queueMicrotask(() => tx.oncomplete?.());
            return tx;
        },
    };
    function result(value) {
        const request = { result: value };
        queueMicrotask(() => request.onsuccess?.({ target: request }));
        return request;
    }
    return { rows, open: () => result(database) };
}

function cacheDouble() {
    const stores = new Map();
    const key = request => new URL(typeof request === 'string' ? request : request.url, origin).href;
    return {
        keys: async () => [...stores.keys()],
        delete: async name => stores.delete(name),
        async open(name) {
            if (!stores.has(name)) stores.set(name, new Map());
            const store = stores.get(name);
            return {
                put: async (request, response) => store.set(key(request), response.clone()),
                match: async request => store.get(key(request))?.clone(),
                addAll: async () => {},
            };
        },
    };
}

function worker(entries = []) {
    const handlers = {};
    const indexedDB = indexedDBDouble(entries);
    const caches = cacheDouble();
    const context = vm.createContext({
        URL, Request, Response, indexedDB, caches,
        location: { origin },
        addEventListener: (name, handler) => { handlers[name] = handler; },
        skipWaiting() {}, clients: { claim: async () => {} },
        fetch: (...args) => context.network(...args),
    });
    context.self = context;
    context.importScripts = () => vm.runInContext(source('offline-security.js'), context);
    context.network = async () => new Response('private', { headers: { 'X-Account-ID': 'alice' } });
    vm.runInContext(source('sw.js'), context);
    return {
        context, caches, indexedDB,
        request(url, options = {}) {
            let response;
            handlers.fetch({ request: new Request(origin + url, options), respondWith: p => { response = p; } });
            return response;
        },
        activate() {
            let completion;
            handlers.activate({ waitUntil: p => { completion = p; } });
            return completion;
        },
    };
}

const offline = () => { throw new TypeError('offline'); };
const asAlice = { headers: { 'X-Account-ID': 'alice' } };

test('offline reads work only for the selected account; logout removes HTML, data and queue', async () => {
    const w = worker();
    await w.request('/');
    await w.request('/api/items', asAlice);
    w.indexedDB.rows.set(1, { accountId: 'alice', method: 'DELETE' });
    w.context.network = offline;
    assert.equal((await w.request('/')).status, 200);
    assert.equal((await w.request('/api/items', asAlice)).status, 200);
    assert.equal((await w.request('/api/items', { headers: { 'X-Account-ID': 'bob' } })).status, 503);
    w.context.network = async () => new Response('', { status: 302 });
    await w.request('/logout', { method: 'POST' });
    assert.equal(w.indexedDB.rows.size, 0);
    assert.ok(!(await w.caches.keys()).some(key => key.startsWith('grocery-private')));
    w.context.network = offline;
    assert.equal((await w.request('/')).status, 503);
    assert.equal((await w.request('/api/items', asAlice)).status, 503);
});

test('a request started before logout cannot repopulate private storage', async () => {
    const w = worker();
    await w.request('/');
    let finish;
    w.context.network = () => new Promise(resolve => { finish = resolve; });
    const pending = w.request('/api/items', asAlice);
    w.context.network = async () => new Response('', { status: 302 });
    await w.request('/logout', { method: 'POST' });
    finish(new Response('secret', { headers: { 'X-Account-ID': 'alice' } }));
    await pending;
    w.context.network = offline;
    assert.equal((await w.request('/api/items', asAlice)).status, 503);
    assert.ok(!(await w.caches.keys()).some(key => key.startsWith('grocery-private')));
});

test('switching accounts removes the old account cache and pending operations', async () => {
    const w = worker();
    await w.request('/');
    await w.request('/api/items', asAlice);
    w.indexedDB.rows.set(1, { accountId: 'alice' });
    w.context.network = async () => new Response('bob page', { headers: { 'X-Account-ID': 'bob' } });
    await w.request('/');
    assert.equal(w.indexedDB.rows.size, 0);
    assert.ok(!(await w.caches.keys()).includes('grocery-private-v2-alice'));
    w.context.network = offline;
    assert.equal(await (await w.request('/')).text(), 'bob page');
    assert.equal((await w.request('/api/items', asAlice)).status, 503);
});

test('activation purges legacy caches and unbound operations', async () => {
    const w = worker([{ method: 'DELETE', url: '/api/items/1' }]);
    await w.caches.open('grocery-v7');
    await w.caches.open('grocery-data-v1');
    await w.caches.open('grocery-static-v8');
    await w.activate();
    assert.deepEqual(await w.caches.keys(), ['grocery-static-v8']);
    assert.equal(w.indexedDB.rows.size, 0);
});

function page(entries, online = true) {
    const indexedDB = indexedDBDouble(entries);
    const calls = [];
    const elements = new Map();
    const element = key => {
        if (!elements.has(key)) elements.set(key, {
            content: key.includes('account-id') ? 'bob' : 'token', value: '',
            style: {}, classList: { toggle() {}, contains() { return false; } },
            listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; },
            setAttribute() {}, appendChild() {},
        });
        return elements.get(key);
    };
    const context = vm.createContext({
        indexedDB, navigator: { onLine: online },
        document: { querySelector: element, getElementById: element, body: element('body'), addEventListener() {} },
        window: { addEventListener() {}, location: {} },
        localStorage: { getItem() { return null; } },
        EventSource: class { addEventListener() {} },
        fetch: async (url, options) => {
            calls.push({ url, ...options });
            return new Response('[]', { headers: { 'Content-Type': 'application/json' } });
        },
    });
    vm.runInContext(source('app.js'), context);
    return { indexedDB, calls, element };
}
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(setImmediate); };

test('queue replay drops legacy and other-account operations and binds surviving writes', async () => {
    const p = page([
        { accountId: 'alice', method: 'DELETE', url: '/api/items/1' },
        { method: 'DELETE', url: '/api/items/2' },
        { accountId: 'bob', method: 'DELETE', url: '/api/items/3' },
    ]);
    await settle();
    const writes = p.calls.filter(call => call.method === 'DELETE');
    assert.equal(writes.length, 1);
    assert.equal(writes[0].url, '/api/items/3');
    assert.equal(writes[0].headers['X-Account-ID'], 'bob');
    assert.equal(p.indexedDB.rows.size, 0);
});

test('new offline operations record the account that rendered the page', async () => {
    const p = page([], false);
    await settle();
    p.element('item-input').value = 'Milk';
    p.element('section-select').value = 'now';
    await p.element('add-btn').listeners.click();
    assert.equal(p.indexedDB.rows.size, 1);
    const op = [...p.indexedDB.rows.values()][0];
    assert.equal(op.accountId, 'bob');
    assert.equal(op.method, 'POST');
    assert.equal(JSON.parse(op.body).name, 'Milk');
});
