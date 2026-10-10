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
                add: value => {
                    const key = Math.max(0, ...rows.keys()) + 1;
                    rows.set(key, value);
                    return { result: key };
                },
                openCursor: () => {
                    const request = {};
                    const keys = [...rows.keys()];
                    let index = 0;
                    const next = () => queueMicrotask(() => {
                        const key = keys[index++];
                        request.result = key === undefined ? null : {
                            value: rows.get(key), delete: () => rows.delete(key), continue: next,
                        };
                        request.onsuccess?.();
                    });
                    next();
                    return request;
                },
                getAll: () => result([...rows.values()]),
                getAllKeys: () => result([...rows.keys()]),
            }) };
            setImmediate(() => tx.oncomplete?.());
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
    await w.caches.open('grocery-static-old');
    await w.caches.open('grocery-static-__ASSET_VERSION__');
    await w.activate();
    assert.deepEqual(await w.caches.keys(), ['grocery-static-__ASSET_VERSION__']);
    assert.equal(w.indexedDB.rows.size, 0);
});

function locksDouble() {
    let pending = Promise.resolve();
    return { request(name, callback) {
        const next = pending.then(callback);
        pending = next.catch(() => {});
        return next;
    } };
}

function page(entries, online = true, options = {}) {
    const indexedDB = options.indexedDB || indexedDBDouble(entries);
    const calls = [];
    const elements = new Map();
    function createElement() {
        return {
            content: '', value: '', style: {}, dataset: {}, children: [],
            classList: { toggle() {}, contains() { return false; } },
            listeners: {}, addEventListener(name, callback) { this.listeners[name] = callback; },
            setAttribute() {}, appendChild(child) { this.children.push(child); },
            set innerHTML(value) { this.children = []; this.html = value; },
            get innerHTML() { return this.html || ''; },
            focus() {}, select() {},
        };
    }
    const element = key => {
        if (!elements.has(key)) {
            const el = createElement();
            el.content = key.includes('account-id') ? 'bob' : 'token';
            elements.set(key, el);
        }
        return elements.get(key);
    };
    const windowEvents = {}, documentEvents = {}, streams = [], timers = [];
    const defaultNetwork = async url => {
        if (url === '/api/lists') return new Response('[{"id":1,"name":"Groceries","can_delete":true},{"id":2,"name":"Costco","can_delete":true}]');
        return new Response('[]', { headers: { 'Content-Type': 'application/json' } });
    };
    const context = vm.createContext({
        indexedDB, navigator: { onLine: online, locks: options.locks || locksDouble() },
        document: {
            querySelector: element, getElementById: element, body: element('body'), createElement,
            addEventListener(name, handler) { documentEvents[name] = handler; },
        },
        window: { addEventListener(name, handler) { windowEvents[name] = handler; }, location: {} },
        localStorage: { getItem() { return null; } },
        EventSource: class {
            constructor() { this.events = {}; streams.push(this); }
            addEventListener(name, handler) { this.events[name] = handler; }
        },
        setTimeout(handler, delay) { timers.push({ handler, delay }); return timers.length; },
        clearTimeout() {},
        fetch: async (url, request) => {
            calls.push({ url, ...request });
            return (options.network || defaultNetwork)(url, request || {});
        },
    });
    vm.runInContext(source('app.js'), context);
    return { indexedDB, calls, element, context, windowEvents, documentEvents, streams, timers };
}

function textOf(element) {
    return [element.textContent || '', ...element.children.map(textOf)].join(' ');
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


test('worker updates preserve current caches and account-bound queued changes', async () => {
    const w = worker([
        { accountId: 'alice', method: 'POST', url: '/api/items', body: '{"name":"Milk"}' },
        { method: 'DELETE', url: '/api/items/1' },
    ]);
    await w.request('/');
    await w.request('/api/items', asAlice);
    // selectAccount clears queues on an account transition; add our pending write afterward.
    w.indexedDB.rows.set(1, { accountId: 'alice', method: 'POST', url: '/api/items' });
    w.indexedDB.rows.set(2, { method: 'DELETE', url: '/api/items/1' });
    await w.activate();
    assert.equal(w.indexedDB.rows.size, 1);
    w.context.network = offline;
    assert.equal((await w.request('/')).status, 200);
    assert.equal((await w.request('/api/items', asAlice)).status, 200);
});

test('overlapping startup, reconnect and visibility flushes replay each write once', async () => {
    const releases = [];
    let writes = 0;
    const p = page([{ accountId: 'bob', method: 'POST', url: '/api/items/1/toggle' }], true, {
        network: async (url, opts) => {
            if (opts.method === 'POST') {
                writes++;
                return new Promise(resolve => releases.push(() => resolve(new Response('{}'))));
            }
            return new Response('[]');
        },
    });
    p.windowEvents.pageshow({ persisted: false });
    p.windowEvents.online();
    p.documentEvents.visibilitychange();
    await settle();
    assert.equal(writes, 1);
    releases.forEach(release => release());
    await settle();
    assert.equal(p.indexedDB.rows.size, 0);
});

test('two tabs serialize queue replay using the origin-wide lock', async () => {
    const indexedDB = indexedDBDouble([{ accountId: 'bob', method: 'POST', url: '/api/items/1/toggle' }]);
    const locks = locksDouble();
    let writes = 0;
    const network = async (url, opts) => {
        if (opts.method === 'POST') writes++;
        return new Response('[]');
    };
    page([], true, { indexedDB, locks, network });
    page([], true, { indexedDB, locks, network });
    await settle();
    assert.equal(writes, 1);
    assert.equal(indexedDB.rows.size, 0);
});

test('rate limiting preserves the queue, honors Retry-After and retries successfully', async () => {
    let limited = true;
    const p = page([{ accountId: 'bob', method: 'POST', url: '/api/items', body: '{"name":"Milk","list_id":1}' }], true, {
        network: async (url, opts) => opts.method === 'POST'
            ? new Response('{}', { status: limited ? 429 : 201, headers: { 'Retry-After': '5' } })
            : new Response('[]'),
    });
    await settle();
    assert.equal(p.indexedDB.rows.size, 1);
    assert.equal(p.timers[0].delay, 5000);
    limited = false;
    p.timers[0].handler();
    await settle();
    assert.equal(p.indexedDB.rows.size, 0);
});

test('offline reload and list switching reconstruct queued changes only on their list', async () => {
    const p = page([{ accountId: 'bob', listId: 1, method: 'POST', url: '/api/items', body: '{"name":"Milk","list_id":1}' }], false);
    await settle();
    assert.match(textOf(p.element('#section-now .item-list')), /Milk/);
    p.element('list-select').value = '2';
    p.element('list-select').listeners.change();
    await settle();
    assert.doesNotMatch(textOf(p.element('#section-now .item-list')), /Milk/);
    p.element('list-select').value = '1';
    p.element('list-select').listeners.change();
    await settle();
    assert.match(textOf(p.element('#section-now .item-list')), /Milk/);
});

test('late list responses cannot overwrite the newly selected list', async () => {
    let finishFirst;
    const p = page([], false, {
        network: async url => {
            if (url === '/api/lists') return new Response('[{"id":1,"name":"A"},{"id":2,"name":"B"}]');
            if (url.endsWith('list_id=1')) return new Promise(resolve => { finishFirst = resolve; });
            if (url.endsWith('list_id=2')) return new Response('[{"id":2,"name":"B item","section":"now"}]');
            return new Response('[]');
        },
    });
    await settle();
    p.element('list-select').value = '2';
    p.element('list-select').listeners.change();
    await settle();
    finishFirst(new Response('[{"id":1,"name":"A item","section":"now"}]'));
    await settle();
    assert.match(textOf(p.element('#section-now .item-list')), /B item/);
    assert.doesNotMatch(textOf(p.element('#section-now .item-list')), /A item/);
});

test('failed additions retain user input and display the server error', async () => {
    const p = page([], true, {
        network: async (url, opts) => opts.method === 'POST'
            ? new Response('{"error":"Notes too long"}', { status: 400 })
            : new Response(url === '/api/lists' ? '[{"id":1,"name":"Groceries"}]' : '[]'),
    });
    await settle();
    p.element('item-input').value = 'Milk';
    p.element('notes-input').value = 'x'.repeat(501);
    await p.element('add-btn').listeners.click();
    assert.equal(p.element('item-input').value, 'Milk');
    assert.equal(p.element('notes-input').value.length, 501);
    assert.match(p.element('app-message').textContent, /Notes too long/);
    assert.equal(p.element('app-message').hidden, false);
});

test('list metadata events recover when another client deletes the selected list', async () => {
    let deleted = false;
    const p = page([], false, {
        network: async url => new Response(url === '/api/lists'
            ? deleted ? '[{"id":2,"name":"Costco"}]' : '[{"id":1,"name":"Groceries"},{"id":2,"name":"Costco"}]'
            : '[]'),
    });
    await settle();
    deleted = true;
    await p.streams[0].events.lists();
    await settle();
    assert.equal(p.element('list-select').value, 2);
    assert.ok(p.calls.some(call => call.url === '/api/items?list_id=2'));
});


test('permanent sync failures keep input and let the user discard only the failed change', async () => {
    const p = page([
        { accountId: 'bob', listId: 1, method: 'POST', url: '/api/items', body: '{"name":"Milk","list_id":1}' },
        { accountId: 'bob', listId: 1, method: 'POST', url: '/api/items', body: '{"name":"Bread","list_id":1}' },
    ], true, {
        network: async (url, opts) => opts.method === 'POST'
            ? new Response(opts.body.includes('Milk') ? '{"error":"List not found"}' : '{}', { status: opts.body.includes('Milk') ? 404 : 201 })
            : new Response(url === '/api/lists' ? '[{"id":1,"name":"Groceries"}]' : '[]'),
    });
    await settle();
    assert.equal(p.indexedDB.rows.size, 2);
    assert.match(p.element('app-message').textContent, /Milk/);
    const discard = p.element('app-message').children.find(button => button.textContent === 'Discard failed change');
    await discard.listeners.click();
    await settle();
    assert.equal(p.indexedDB.rows.size, 0);
    const writes = p.calls.filter(call => call.method === 'POST');
    assert.equal(writes.length, 2);
    assert.match(writes[1].body, /Bread/);
});
