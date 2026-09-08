CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL DEFAULT (lower(hex(randomblob(16)))) UNIQUE,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    is_active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    session_epoch INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    section TEXT NOT NULL CHECK (section IN ('now', 'later')),
    is_bought INTEGER DEFAULT 0,
    quantity TEXT,
    notes TEXT,
    list_id INTEGER REFERENCES lists(id),
    added_by INTEGER REFERENCES users(id),
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS item_name_history (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    last_used TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT (datetime('now')),
    actor_id INTEGER REFERENCES users(id),
    actor_username TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
