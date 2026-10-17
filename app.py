import functools
import hashlib
import hmac
import ipaddress
import json
import math
import queue
import re
import socket
import sqlite3
import threading
import time
from datetime import date, timedelta
from functools import wraps
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import anthropic
import bcrypt
import requests
from requests.adapters import HTTPAdapter
from flask import (
    Flask,
    Response,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import ingredients as ingr

app = Flask(__name__)
app.config.from_object(config)  # SECRET_KEY, WTF_CSRF_TIME_LIMIT
if config.TRUST_PROXY_HEADERS:
    # The Compose deployment accepts traffic only from one trusted reverse proxy
    # hop (cloudflared). Do not enable this when clients can reach Gunicorn
    # directly, because forwarding headers are otherwise spoofable.
    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=config.TRUSTED_PROXY_HOPS,
        x_proto=config.TRUSTED_PROXY_HOPS,
    )
app.permanent_session_lifetime = timedelta(days=36500)
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = True

csrf = CSRFProtect(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per minute"],
    # NOTE: memory:// storage only works correctly with --workers 1 (current Dockerfile setting).
    # Switch to redis:// if worker count is ever increased.
    storage_uri="memory://",
)

# SSE subscribers: list of queue.Queue objects
subscribers = []
subscribers_lock = threading.Lock()


@app.before_request
def require_https():
    # The container health check calls /healthz directly over loopback HTTP.
    if request.path == "/healthz":
        return None
    if config.ENFORCE_HTTPS and not request.is_secure:
        return redirect(request.url.replace("http://", "https://", 1), code=308)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

# bcrypt only uses the first 72 bytes. bcrypt<5 truncated silently; bcrypt>=5
# raises instead, so truncate explicitly to keep existing hashes verifiable.
BCRYPT_MAX_BYTES = 72


def hash_password(password):
    return bcrypt.hashpw(password.encode()[:BCRYPT_MAX_BYTES], bcrypt.gensalt()).decode()


def check_password(password, pw_hash):
    return bcrypt.checkpw(password.encode()[:BCRYPT_MAX_BYTES], pw_hash)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(config.DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'"
    )
    if not request.path.startswith("/static/") and request.endpoint != "service_worker":
        response.headers["Cache-Control"] = "no-store"
    if getattr(g, "user", None) is not None:
        response.headers["X-Account-ID"] = g.user["account_id"]
    return response


def init_db():
    db = sqlite3.connect(config.DATABASE)
    with open("schema.sql") as f:
        db.executescript(f.read())
    db.close()


def upgrade_db():
    db = sqlite3.connect(config.DATABASE)

    # --- User column upgrades (legacy) ---
    cols = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if 'role' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    if 'is_active' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if 'must_change_password' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
    if 'session_epoch' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")

    # A random, immutable identity prevents deleted-row ID reuse from reviving
    # cookies. Legacy cookies lack this value and must authenticate again.
    if 'account_id' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN account_id TEXT")
    db.execute("UPDATE users SET account_id = lower(hex(randomblob(16))) WHERE account_id IS NULL")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_account_id ON users(account_id)")
    # ALTER TABLE cannot add a random expression default to a populated table.
    # The trigger also covers future inserts into upgraded databases.
    db.execute("""
        CREATE TRIGGER IF NOT EXISTS users_assign_account_id AFTER INSERT ON users
        WHEN NEW.account_id IS NULL
        BEGIN
            UPDATE users SET account_id = lower(hex(randomblob(16))) WHERE id = NEW.id;
        END
    """)

    # --- Lists table (old DBs won't have it) ---
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'lists' not in tables:
        db.execute("""
            CREATE TABLE lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_by INTEGER REFERENCES users(id),
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

    # Ensure at least one list exists
    if db.execute("SELECT COUNT(*) FROM lists").fetchone()[0] == 0:
        db.execute("INSERT INTO lists (name) VALUES ('Groceries')")

    # Adopt ownerless lists — the seeded 'Groceries' list and anything created
    # before created_by was populated — into the first admin account, so that
    # admin can delete them under the ownership rules in delete_list(). On a
    # brand-new install there is no admin yet; register() does the same claim
    # once the initial account is created.
    db.execute(
        "UPDATE lists SET created_by = ("
        "  SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ") WHERE created_by IS NULL"
        "  AND EXISTS (SELECT 1 FROM users WHERE role = 'admin')"
    )
    db.commit()

    # --- Item column upgrades ---
    item_cols = {row[1] for row in db.execute("PRAGMA table_info(items)")}
    if 'quantity' not in item_cols:
        db.execute("ALTER TABLE items ADD COLUMN quantity TEXT")
    if 'notes' not in item_cols:
        db.execute("ALTER TABLE items ADD COLUMN notes TEXT")
    if 'list_id' not in item_cols:
        db.execute("ALTER TABLE items ADD COLUMN list_id INTEGER REFERENCES lists(id)")
        db.execute("UPDATE items SET list_id = (SELECT id FROM lists ORDER BY id LIMIT 1) WHERE list_id IS NULL")

    # --- Item name history table ---
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'item_name_history' not in tables:
        db.execute("""
            CREATE TABLE item_name_history (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                last_used TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("INSERT OR IGNORE INTO item_name_history (name) SELECT DISTINCT name FROM items")

    # Recipe and meal-planning tables (recipes, recipe_ingredients,
    # planned_meals, kids, kid_lunches) come from schema.sql, which init_db
    # runs just before this. The week menu added one column to planned_meals.
    meal_cols = {row[1] for row in db.execute("PRAGMA table_info(planned_meals)")}
    if meal_cols and 'unscheduled' not in meal_cols:
        db.execute("ALTER TABLE planned_meals ADD COLUMN unscheduled INTEGER NOT NULL DEFAULT 0")
        db.commit()

    # --- Audit log table ---
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'audit_log' not in tables:
        db.execute("""
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT DEFAULT (datetime('now')),
                actor_id INTEGER REFERENCES users(id),
                actor_username TEXT NOT NULL,
                action TEXT NOT NULL,
                detail TEXT
            )
        """)

    db.commit()
    db.close()


def has_users():
    db = get_db()
    return db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def auth_failure():
    # fetch() follows redirects transparently, so API clients can't detect a
    # 302-to-login. Return 401 JSON for API paths so the frontend can react.
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    return redirect(url_for("login"))


def validate_session():
    user = get_db().execute(
        "SELECT id, role, is_active, session_epoch, account_id, must_change_password "
        "FROM users WHERE id = ?", (session.get("user_id"),)
    ).fetchone()
    if (not user or user["is_active"] != 1
            or session.get("epoch") != user["session_epoch"]
            or session.get("account_id") != user["account_id"]):
        session.clear()
        return auth_failure()
    g.user = user
    # Bind browser writes to the account that rendered the page. This also
    # protects old tabs and offline queues after another account signs in.
    expected_account = request.headers.get("X-Account-ID")
    requires_account = request.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS")
    if (requires_account or expected_account is not None) and expected_account != user["account_id"]:
        return jsonify({"error": "account_changed"}), 409
    if user["must_change_password"] and request.endpoint not in ("change_password", "csrf_token"):
        if request.path.startswith("/api/"):
            return jsonify({"error": "password_change_required"}), 403
        return redirect(url_for("change_password"))
    return None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        failure = validate_session()
        if failure is not None:
            return failure
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if g.user["role"] != "admin":
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


def current_user_is_admin():
    # Read the role from the DB rather than the session: roles can change after
    # a cookie is issued, and admin_required does the same live lookup.
    row = get_db().execute(
        "SELECT role FROM users WHERE id = ?", (session.get("user_id"),)
    ).fetchone()
    return row is not None and row["role"] == "admin"


def may_delete_list(created_by, is_admin):
    # Lists created before per-list ownership existed have a NULL created_by,
    # so only an admin can delete those.
    return is_admin or (created_by is not None and created_by == session.get("user_id"))


def safe_referrer():
    # The Referer header is request-controlled; redirecting to it blindly is an
    # open redirect. Only follow it back to our own host.
    ref = request.referrer
    if ref:
        parts = urlparse(ref)
        if parts.scheme in ("http", "https") and parts.netloc == request.host:
            return ref
    return None


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    if request.path.startswith("/api/"):
        # Recognizable shape so the frontend can refresh its token and retry
        return jsonify({"error": "csrf", "message": e.description}), 400
    flash("Your session expired — please try again.")
    return redirect(safe_referrer() or url_for("index"))


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------

def broadcast(event_type, data=None):
    msg = f"event: {event_type}\ndata: {json.dumps(data or {})}\n\n"
    with subscribers_lock:
        dead = []
        for q in subscribers:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            subscribers.remove(q)


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def log_audit(action, detail=None):
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (actor_id, actor_username, action, detail) VALUES (?, ?, ?, ?)",
        (session.get("user_id"), session.get("username", "system"), action, detail),
    )


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def register():
    # Only accessible for initial setup (no users yet)
    if has_users():
        return redirect(url_for("login"))

    bootstrap_enabled = bool(config.BOOTSTRAP_TOKEN)
    if not bootstrap_enabled:
        return render_template("register.html", bootstrap_enabled=False), 503

    if request.method == "GET":
        return render_template("register.html", bootstrap_enabled=True)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    setup_token = request.form.get("setup_token", "")

    # Compare bytes: compare_digest raises TypeError on non-ASCII str input.
    if not hmac.compare_digest(setup_token.encode(), config.BOOTSTRAP_TOKEN.encode()):
        flash("Invalid setup token.")
        return render_template("register.html", bootstrap_enabled=True), 403

    if not username or not password:
        flash("Username and password are required.")
        return render_template("register.html", bootstrap_enabled=True), 400
    if len(password) < 5:
        flash("Password must be at least 5 characters.")
        return render_template("register.html", bootstrap_enabled=True), 400

    pw_hash = hash_password(password)
    db = get_db()
    # Serialize setup and re-check inside the write transaction. This prevents
    # two simultaneous first-run requests from both creating administrators.
    db.execute("BEGIN IMMEDIATE")
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] != 0:
        db.rollback()
        return redirect(url_for("login"))
    db.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
        (username, pw_hash),
    )
    user = db.execute("SELECT id, role, session_epoch, account_id FROM users WHERE username = ?", (username,)).fetchone()
    session["user_id"] = user["id"]
    session["username"] = username
    session["role"] = user["role"]
    session["epoch"] = user["session_epoch"]
    session["account_id"] = user["account_id"]
    session["must_change_password"] = False
    # The seeded list is created before any user exists, so hand it to the
    # initial admin (see the matching backfill in upgrade_db).
    db.execute("UPDATE lists SET created_by = ? WHERE created_by IS NULL", (user["id"],))
    log_audit("user.register", f"Initial admin account '{username}' created")
    db.commit()
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def login():
    if not has_users():
        return redirect(url_for("register"))

    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    # Always run bcrypt to prevent username enumeration via timing
    dummy_hash = b"$2b$12$GhvMmNVjRW29ulnudl.LbuAnUtN/LRfe1JsBm1Vf3nGJM9XuQ.i51"
    candidate_hash = user["password_hash"].encode() if user else dummy_hash
    password_ok = check_password(password, candidate_hash)

    if user and password_ok:
        if user["is_active"] != 1:
            flash("Your account has been disabled.")
            return render_template("login.html"), 401
        session.clear()                            # prevent session fixation
        session.permanent = request.form.get("remember_me") == "on"
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        session["epoch"] = user["session_epoch"]
        session["account_id"] = user["account_id"]
        session["must_change_password"] = bool(user["must_change_password"])
        if user["must_change_password"]:
            return redirect(url_for("change_password"))
        return redirect(url_for("index"))

    flash("Invalid username or password.")
    return render_template("login.html"), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    if not has_users():
        return redirect(url_for("register"))
    return render_template("list.html", username=session["username"],
                           is_admin=(session.get("role") == "admin"))


@app.route("/recipes")
@app.route("/recipes/<int:recipe_id>")
@login_required
def recipes(recipe_id=None):
    return render_template("recipes.html", username=session["username"],
                           is_admin=(session.get("role") == "admin"),
                           import_url_enabled=bool(config.ANTHROPIC_API_KEY),
                           recipe_id=recipe_id)


@app.route("/meals")
@login_required
def meals():
    return render_template("meals.html", username=session["username"],
                           is_admin=(session.get("role") == "admin"))


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/csrf-token")
@login_required
def csrf_token():
    # Lets a long-running PWA page refresh its CSRF token without a full reload
    return jsonify({"token": generate_csrf()})


@app.route("/api/lists")
@login_required
def get_lists():
    db = get_db()
    rows = db.execute("SELECT id, name, created_by FROM lists ORDER BY id").fetchall()
    is_admin = current_user_is_admin()
    return jsonify([
        {"id": r["id"], "name": r["name"],
         "can_delete": may_delete_list(r["created_by"], is_admin)}
        for r in rows
    ])


@app.route("/api/lists", methods=["POST"])
@login_required
def create_list():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("name", ""), str):
        return jsonify({"error": "Name must be a string"}), 400
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if len(name) > 100:
        return jsonify({"error": "Name too long"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO lists (name, created_by) VALUES (?, ?)",
        (name, session["user_id"]),
    )
    db.commit()
    broadcast("lists")
    return jsonify({"id": cur.lastrowid, "name": name}), 201


@app.route("/api/lists/<int:list_id>", methods=["DELETE"])
@login_required
def delete_list(list_id):
    db = get_db()
    lst = db.execute(
        "SELECT name, created_by FROM lists WHERE id = ?", (list_id,)
    ).fetchone()
    if lst is None:
        return jsonify({"error": "List not found"}), 404
    if not may_delete_list(lst["created_by"], current_user_is_admin()):
        return jsonify({"error": "Only an admin or the list's creator can delete it"}), 403
    count = db.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
    if count <= 1:
        return jsonify({"error": "Cannot delete the last list"}), 400
    db.execute("DELETE FROM items WHERE list_id = ?", (list_id,))
    db.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    log_audit("list.delete", f"Deleted list '{lst['name']}'")
    db.commit()
    broadcast("lists")
    return jsonify({"ok": True})


@app.route("/api/items/history")
@login_required
def item_history():
    db = get_db()
    rows = db.execute(
        "SELECT name FROM item_name_history ORDER BY name COLLATE NOCASE"
    ).fetchall()
    return jsonify([r["name"] for r in rows])


@app.route("/api/items/history/<string:name>", methods=["DELETE"])
@login_required
def delete_history_item(name):
    db = get_db()
    db.execute("DELETE FROM item_name_history WHERE name = ? COLLATE NOCASE", (name,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/items")
@login_required
def get_items():
    list_id = request.args.get("list_id", type=int)
    db = get_db()
    if list_id is None:
        first = db.execute("SELECT id FROM lists ORDER BY id LIMIT 1").fetchone()
        list_id = first["id"] if first else None
    if list_id is None:
        return jsonify([])
    rows = db.execute(
        "SELECT items.*, users.username AS added_by_name "
        "FROM items LEFT JOIN users ON items.added_by = users.id "
        "WHERE items.list_id = ? "
        "ORDER BY items.is_bought ASC, items.created_at DESC",
        (list_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


def _validate_item(data):
    """Normalize and validate an item payload. Returns (fields, error)."""
    if not isinstance(data, dict):
        return None, "Item must be an object"
    for key in ("name", "quantity", "notes"):
        if data.get(key) is not None and not isinstance(data[key], str):
            return None, f"{key} must be a string"
    name = (data.get("name") or "").strip()
    section = data.get("section", "now")
    quantity = (data.get("quantity") or "").strip() or None
    notes = (data.get("notes") or "").strip() or None

    if not name:
        return None, "Name is required"
    if len(name) > 200:
        return None, "Name too long"
    if quantity and len(quantity) > 50:
        return None, "Quantity too long"
    if notes and len(notes) > 500:
        return None, "Notes too long"
    if section not in ("now", "later"):
        return None, "Section must be 'now' or 'later'"
    return {"name": name, "section": section, "quantity": quantity, "notes": notes}, None


def _default_list_id(db):
    first = db.execute("SELECT id FROM lists ORDER BY id LIMIT 1").fetchone()
    return first["id"] if first else None


def _insert_item(db, fields, list_id, added_by):
    """Insert a validated item and record its name for autocomplete. No commit."""
    cur = db.execute(
        "INSERT INTO items (name, section, quantity, notes, list_id, added_by) VALUES (?, ?, ?, ?, ?, ?)",
        (fields["name"], fields["section"], fields["quantity"], fields["notes"], list_id, added_by),
    )
    db.execute(
        "INSERT INTO item_name_history (name, last_used) VALUES (?, datetime('now')) "
        "ON CONFLICT(name) DO UPDATE SET last_used = datetime('now')",
        (fields["name"],),
    )
    return cur.lastrowid


def _fetch_item(db, item_id):
    return db.execute(
        "SELECT items.*, users.username AS added_by_name "
        "FROM items LEFT JOIN users ON items.added_by = users.id "
        "WHERE items.id = ?",
        (item_id,),
    ).fetchone()


@app.route("/api/items", methods=["POST"])
@login_required
def add_item():
    data = request.get_json(silent=True)
    fields, error = _validate_item(data)
    if error:
        return jsonify({"error": error}), 400

    db = get_db()
    list_id = data.get("list_id")
    if list_id is None:
        list_id = _default_list_id(db)

    if type(list_id) is not int or db.execute(
        "SELECT 1 FROM lists WHERE id = ?", (list_id,)
    ).fetchone() is None:
        return jsonify({"error": "List not found"}), 404

    item_id = _insert_item(db, fields, list_id, session["user_id"])
    db.commit()

    broadcast("update")
    return jsonify(dict(_fetch_item(db, item_id))), 201


# NOTE: Item and list mutations below intentionally allow any authenticated user to
# modify any item or list. This app is designed for household shared access — all
# users collaborate on the same lists with equal write permissions. There is no
# per-list membership model by design. If private lists are ever added, these
# endpoints will need ownership checks before that feature ships.
#
# Deleting a whole list is the one exception: it is destructive and irreversible,
# so it is restricted to admins and the list's creator (see delete_list above).

@app.route("/api/items/<int:item_id>/toggle", methods=["POST"])
@login_required
def toggle_item(item_id):
    db = get_db()
    db.execute(
        "UPDATE items SET is_bought = NOT is_bought, updated_at = datetime('now') WHERE id = ?",
        (item_id,),
    )
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


@app.route("/api/items/<int:item_id>/move", methods=["POST"])
@login_required
def move_item(item_id):
    db = get_db()
    db.execute(
        "UPDATE items SET section = CASE WHEN section = 'now' THEN 'later' ELSE 'now' END, "
        "updated_at = datetime('now') WHERE id = ?",
        (item_id,),
    )
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


@app.route("/api/items/<int:item_id>", methods=["PUT"])
@login_required
def update_item(item_id):
    fields, error = _validate_item(request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    name, section = fields["name"], fields["section"]
    quantity, notes = fields["quantity"], fields["notes"]

    db = get_db()
    if db.execute("SELECT id FROM items WHERE id = ?", (item_id,)).fetchone() is None:
        return jsonify({"error": "Not found"}), 404

    # is_bought is intentionally left untouched -- editing details shouldn't
    # un-buy an item.
    db.execute(
        "UPDATE items SET name = ?, section = ?, quantity = ?, notes = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (name, section, quantity, notes, item_id),
    )
    db.execute(
        "INSERT INTO item_name_history (name, last_used) VALUES (?, datetime('now')) "
        "ON CONFLICT(name) DO UPDATE SET last_used = datetime('now')",
        (name,),
    )
    db.commit()

    item = db.execute(
        "SELECT items.*, users.username AS added_by_name "
        "FROM items LEFT JOIN users ON items.added_by = users.id "
        "WHERE items.id = ?",
        (item_id,),
    ).fetchone()

    broadcast("update")
    return jsonify(dict(item))


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
@login_required
def delete_item(item_id):
    db = get_db()
    item = db.execute("SELECT name FROM items WHERE id = ?", (item_id,)).fetchone()
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    log_audit("item.delete", f"Deleted item '{item['name'] if item else item_id}'")
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


@app.route("/api/items/clear-bought", methods=["POST"])
@login_required
def clear_bought():
    data = request.get_json() or {}
    list_id = data.get("list_id")
    db = get_db()
    if list_id:
        lst = db.execute("SELECT name FROM lists WHERE id = ?", (list_id,)).fetchone()
        db.execute("DELETE FROM items WHERE is_bought = 1 AND list_id = ?", (list_id,))
        log_audit("items.clear_bought", f"Cleared bought items from list '{lst['name'] if lst else list_id}'")
    else:
        db.execute("DELETE FROM items WHERE is_bought = 1")
        log_audit("items.clear_bought", "Cleared all bought items")
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Recipes API
# ---------------------------------------------------------------------------
#
# Recipes and their edits are open to any household member, same as items and
# lists (see the note above add_item). Deletion is restricted to the recipe's
# creator or an admin, same as delete_list.
#
# Ingredients are stored structured (name / qty / unit / note) so that meals
# can be scaled to a serving count and combined across recipes when they are
# added to a grocery list. See ingredients.py for the unit handling.

MAX_RECIPE_INGREDIENTS = 100
MAX_INGREDIENT_QTY = 100_000
MAX_SERVINGS = 100


def may_delete_recipe(created_by, is_admin):
    return is_admin or (created_by is not None and created_by == session.get("user_id"))


def _prepare_lines(raw, max_lines, max_len):
    """Trim a list of freeform text lines, dropping blanks, for storage.

    Returns (cleaned_list, error_message). error_message is None on success.
    """
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        return None, "Invalid format"
    if len(raw) > max_lines:
        return None, f"Too many lines (max {max_lines})"
    cleaned = []
    for entry in raw:
        text = entry.strip() if isinstance(entry, str) else ""
        if not text:
            continue
        if len(text) > max_len:
            return None, f"Line too long (max {max_len} chars)"
        cleaned.append(text)
    return cleaned, None


def _valid_servings(value, allow_none=False):
    if value is None and allow_none:
        return True
    return type(value) is int and 1 <= value <= MAX_SERVINGS


def _validate_ingredient(raw):
    """Normalize one ingredient: a free-text line or a {name, qty, unit, note} object."""
    if isinstance(raw, str):
        if not raw.strip():
            return None, None
        if len(raw) > 300:
            return None, "Ingredient line too long (max 300 chars)"
        raw = ingr.parse_ingredient_line(raw)
    if not isinstance(raw, dict):
        return None, "Invalid ingredient"
    for key in ("name", "unit", "note"):
        if raw.get(key) is not None and not isinstance(raw[key], str):
            return None, f"Ingredient {key} must be a string"
    name = (raw.get("name") or "").strip()
    unit = (raw.get("unit") or "").strip()
    note = (raw.get("note") or "").strip()
    qty = raw.get("qty")
    if isinstance(qty, str):
        # Amounts typed into the editor arrive as text: "1 1/2", "½".
        qty = ingr.parse_qty(qty)
    if isinstance(qty, bool) or (qty is not None and not isinstance(qty, (int, float))):
        return None, "Ingredient amount must be a number"
    if qty is not None and (math.isnan(qty) or not 0 < qty <= MAX_INGREDIENT_QTY):
        return None, f"Couldn't read the amount for '{name or 'an ingredient'}'"
    if not name:
        # A row with only an amount is a half-filled editor row, not an error.
        return None, None
    if len(name) > 200:
        return None, "Ingredient name too long"
    if len(unit) > 40:
        return None, "Ingredient unit too long"
    if len(note) > 500:
        return None, "Ingredient note too long"
    return {"name": name, "qty": float(qty) if qty is not None else None, "unit": unit, "note": note}, None


def _validate_recipe_payload(data):
    """Shared validation for create/update. Returns (fields_dict, error_response_or_None)."""
    if not isinstance(data, dict):
        return None, (jsonify({"error": "Invalid format"}), 400)
    name = (data.get("name") or "").strip() if isinstance(data.get("name"), (str, type(None))) else ""
    notes = data.get("notes")
    notes = notes.strip() or None if isinstance(notes, str) else None

    if not name:
        return None, (jsonify({"error": "Name is required"}), 400)
    if len(name) > 200:
        return None, (jsonify({"error": "Name too long"}), 400)
    if notes and len(notes) > 2000:
        return None, (jsonify({"error": "Notes too long"}), 400)

    servings = data.get("servings", 4)
    if not _valid_servings(servings):
        return None, (jsonify({"error": f"Servings must be a whole number from 1 to {MAX_SERVINGS}"}), 400)

    source_url = data.get("source_url")
    source_url = source_url.strip() or None if isinstance(source_url, str) else None
    if source_url:
        parts = urlparse(source_url)
        if len(source_url) > 2048 or parts.scheme not in ("http", "https") or not parts.netloc:
            return None, (jsonify({"error": "Source link must be an http(s) URL"}), 400)

    raw_ingredients = data.get("ingredients")
    if not isinstance(raw_ingredients, list):
        return None, (jsonify({"error": "Invalid format"}), 400)
    if len(raw_ingredients) > MAX_RECIPE_INGREDIENTS:
        return None, (jsonify({"error": f"Too many ingredients (max {MAX_RECIPE_INGREDIENTS})"}), 400)
    ingredients = []
    for raw in raw_ingredients:
        ingredient, err = _validate_ingredient(raw)
        if err:
            return None, (jsonify({"error": err}), 400)
        if ingredient:
            ingredients.append(ingredient)
    if not ingredients:
        return None, (jsonify({"error": "At least one ingredient is required"}), 400)

    steps, err = _prepare_lines(data.get("steps"), max_lines=100, max_len=300)
    if err:
        return None, (jsonify({"error": err}), 400)
    steps_text = "\n".join(steps) or None
    if steps_text and len(steps_text) > 4000:
        return None, (jsonify({"error": "Steps too long"}), 400)

    return {
        "name": name, "notes": notes, "steps_text": steps_text, "servings": servings,
        "source_url": source_url, "ingredients": ingredients,
    }, None


def _write_ingredients(db, recipe_id, ingredients):
    db.executemany(
        "INSERT INTO recipe_ingredients (recipe_id, name, qty, unit, note, sort) VALUES (?, ?, ?, ?, ?, ?)",
        [(recipe_id, i["name"], i["qty"], i["unit"], i["note"], sort) for sort, i in enumerate(ingredients)],
    )


def _recipe_ingredients(db, recipe_ids):
    # json_each() binds the whole id list as one parameter, so the SQL text
    # never has to be assembled from the input.
    return db.execute(
        "SELECT id, recipe_id, name, qty, unit, note FROM recipe_ingredients "
        "WHERE recipe_id IN (SELECT value FROM json_each(?)) ORDER BY recipe_id, sort, id",
        (json.dumps(list(recipe_ids)),),
    ).fetchall()


@app.route("/api/recipes")
@login_required
def get_recipes():
    query = (request.args.get("q") or "").strip()[:100]
    db = get_db()
    sql = (
        "SELECT recipes.*, "
        "(SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id = recipes.id) AS ingredient_count "
        "FROM recipes "
    )
    params = ()
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += "WHERE name LIKE ? ESCAPE '\\' COLLATE NOCASE OR notes LIKE ? ESCAPE '\\' COLLATE NOCASE "
        params = (f"%{escaped}%", f"%{escaped}%")
    rows = db.execute(sql + "ORDER BY name COLLATE NOCASE", params).fetchall()
    is_admin = current_user_is_admin()
    return jsonify([
        {
            "id": r["id"],
            "name": r["name"],
            "notes": r["notes"],
            "servings": r["servings"],
            "ingredient_count": r["ingredient_count"],
            "created_by": r["created_by"],
            "can_delete": may_delete_recipe(r["created_by"], is_admin),
        }
        for r in rows
    ])


def _editable_qty(qty):
    """The amount as the editor should show it: "1½" when that reads back
    exactly, otherwise plain decimals so saving never changes the amount."""
    if qty is None:
        return ""
    text = ingr.format_qty(qty)
    if abs((ingr.parse_qty(text) or 0) - qty) < 1e-9:
        return text
    return f"{qty:.3f}".rstrip("0").rstrip(".")


@app.route("/api/recipes/<int:recipe_id>")
@login_required
def get_recipe(recipe_id):
    db = get_db()
    recipe = db.execute(
        "SELECT recipes.*, users.username AS created_by_name "
        "FROM recipes LEFT JOIN users ON recipes.created_by = users.id "
        "WHERE recipes.id = ?",
        (recipe_id,),
    ).fetchone()
    if recipe is None:
        return jsonify({"error": "Recipe not found"}), 404
    servings = request.args.get("servings", type=int)
    if not _valid_servings(servings):
        servings = recipe["servings"]
    ingredients = []
    for row in _recipe_ingredients(db, [recipe_id]):
        scaled = ingr.scale_qty(row["qty"], recipe["servings"], servings)
        ingredients.append({
            "id": row["id"], "name": row["name"], "qty": row["qty"], "unit": row["unit"], "note": row["note"],
            "qty_text": _editable_qty(row["qty"]),
            # Scaled and formatted server-side so the page has one source of truth
            # for kitchen fractions ("1½ cups").
            "amount": ingr.format_quantity(scaled, row["unit"]) if scaled is not None else "",
        })
    data = dict(recipe)
    data["ingredients"] = ingredients
    data["scaled_servings"] = servings
    data["can_delete"] = may_delete_recipe(recipe["created_by"], current_user_is_admin())
    return jsonify(data)


@app.route("/api/recipes/parse-ingredients", methods=["POST"])
@login_required
def parse_ingredients():
    """Split pasted ingredient lines into editor fields. Never writes."""
    data = request.get_json(silent=True)
    lines = data.get("lines") if isinstance(data, dict) else None
    lines, err = _prepare_lines(lines, max_lines=MAX_RECIPE_INGREDIENTS, max_len=300)
    if err:
        return jsonify({"error": err}), 400
    parsed = []
    for line in lines:
        item = ingr.parse_ingredient_line(line)
        parsed.append({**item, "qty_text": ingr.format_qty(item["qty"]) if item["qty"] is not None else ""})
    return jsonify({"ingredients": parsed})


@app.route("/api/recipes", methods=["POST"])
@login_required
def create_recipe():
    fields, error = _validate_recipe_payload(request.get_json(silent=True))
    if error:
        return error

    db = get_db()
    with db:
        cur = db.execute(
            "INSERT INTO recipes (name, notes, steps, servings, source_url, created_by) VALUES (?, ?, ?, ?, ?, ?)",
            (fields["name"], fields["notes"], fields["steps_text"], fields["servings"],
             fields["source_url"], session["user_id"]),
        )
        recipe_id = cur.lastrowid
        _write_ingredients(db, recipe_id, fields["ingredients"])
    broadcast("update")
    return jsonify({"id": recipe_id}), 201


@app.route("/api/recipes/<int:recipe_id>", methods=["PUT"])
@login_required
def update_recipe(recipe_id):
    db = get_db()
    if db.execute("SELECT id FROM recipes WHERE id = ?", (recipe_id,)).fetchone() is None:
        return jsonify({"error": "Recipe not found"}), 404

    fields, error = _validate_recipe_payload(request.get_json(silent=True))
    if error:
        return error

    with db:
        db.execute(
            "UPDATE recipes SET name = ?, notes = ?, steps = ?, servings = ?, source_url = ?, "
            "updated_at = datetime('now') WHERE id = ?",
            (fields["name"], fields["notes"], fields["steps_text"], fields["servings"],
             fields["source_url"], recipe_id),
        )
        db.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
        _write_ingredients(db, recipe_id, fields["ingredients"])
    broadcast("update")
    return jsonify({"ok": True})


@app.route("/api/recipes/<int:recipe_id>", methods=["DELETE"])
@login_required
def delete_recipe(recipe_id):
    db = get_db()
    recipe = db.execute(
        "SELECT name, created_by FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    if recipe is None:
        return jsonify({"error": "Recipe not found"}), 404
    if not may_delete_recipe(recipe["created_by"], current_user_is_admin()):
        return jsonify({"error": "Only an admin or the recipe's creator can delete it"}), 403
    # Planned meals keep their title; recipe_id is cleared by ON DELETE SET NULL.
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    log_audit("recipe.delete", f"Deleted recipe '{recipe['name']}'")
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Meal planning API
# ---------------------------------------------------------------------------
#
# A planned meal sits on a plain YYYY-MM-DD date in the household's local
# calendar. The server never works out "today" -- the browser sends the dates
# it means -- so a meal planned for Tuesday stays on Tuesday whatever time
# zone the server runs in. Kinds follow MenuPlanner: a "cook" night uses a
# recipe (and is the only kind whose ingredients go to the list), "leftovers"
# eats up an earlier cook night, and "flexible" is a loose, easy night.
#
# A meal can also sit on the week's menu without a day ("unscheduled"): a pick
# list of what to make that week. It is stored on the week's Monday so range
# queries find it, and gets a real date once someone picks a day for it.

MEAL_KINDS = ("cook", "leftovers", "flexible")
MAX_MEAL_RANGE_DAYS = 42


def _parse_iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _fetch_meals(db, ids=None, start=None, end=None):
    """Meals with their recipe and leftovers details, by id list and/or date range."""
    return db.execute(
        "SELECT m.*, r.name AS recipe_name, r.servings AS recipe_servings, "
        "l.title AS leftover_of_title, l.date AS leftover_of_date, lists.name AS added_to_list_name "
        "FROM planned_meals m "
        "LEFT JOIN recipes r ON m.recipe_id = r.id "
        "LEFT JOIN planned_meals l ON m.leftover_of = l.id "
        "LEFT JOIN lists ON m.added_to_list_id = lists.id "
        "WHERE (?1 IS NULL OR m.id IN (SELECT value FROM json_each(?1))) "
        "AND (?2 IS NULL OR m.date >= ?2) AND (?3 IS NULL OR m.date <= ?3) "
        "ORDER BY m.date, m.sort, m.id",
        (json.dumps(ids) if ids is not None else None, start, end),
    ).fetchall()


def _validate_meal(db, data):
    """Normalize a meal payload. Returns (fields, error_message)."""
    if not isinstance(data, dict):
        return None, "Invalid format"
    meal_date = _parse_iso_date(data.get("date"))
    if meal_date is None:
        return None, "Date must be YYYY-MM-DD"
    kind = data.get("kind", "cook")
    if kind not in MEAL_KINDS:
        return None, "Kind must be cook, leftovers or flexible"
    for key in ("title", "notes"):
        if data.get(key) is not None and not isinstance(data[key], str):
            return None, f"{key} must be a string"
    title = (data.get("title") or "").strip()
    notes = (data.get("notes") or "").strip() or None
    if len(title) > 200:
        return None, "Title too long"
    if notes and len(notes) > 500:
        return None, "Notes too long"
    servings = data.get("servings")
    if not _valid_servings(servings, allow_none=True):
        return None, f"Servings must be a whole number from 1 to {MAX_SERVINGS}"
    unscheduled = data.get("unscheduled", False)
    if not isinstance(unscheduled, bool):
        return None, "unscheduled must be true or false"
    if unscheduled:
        if kind == "leftovers":
            return None, "Leftovers need a day"
        meal_date -= timedelta(days=meal_date.weekday())

    recipe_id = data.get("recipe_id")
    recipe = None
    if recipe_id is not None:
        if type(recipe_id) is not int:
            return None, "Invalid recipe"
        recipe = db.execute("SELECT id, name FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
        if recipe is None:
            return None, "Recipe not found"

    leftover_of = data.get("leftover_of")
    leftover_meal = None
    if kind != "leftovers":
        leftover_of = None
    elif leftover_of is not None:
        if type(leftover_of) is not int:
            return None, "Invalid cook night"
        leftover_meal = db.execute(
            "SELECT id, title, date, unscheduled FROM planned_meals WHERE id = ? AND kind = 'cook'", (leftover_of,)
        ).fetchone()
        if leftover_meal is None:
            return None, "Leftovers must point at a cook night"
        if leftover_meal["date"] > meal_date.isoformat():
            return None, "Leftovers can't come before the meal they're from"
        if leftover_meal["unscheduled"]:
            return None, "Pick a day for that cook night first"

    if not title:
        if recipe is not None:
            title = recipe["name"]
        elif leftover_meal is not None:
            title = f"Leftover {leftover_meal['title']}"[:200]
        else:
            return None, "Pick a recipe or type what's for dinner"
    return {
        "date": meal_date.isoformat(), "unscheduled": int(unscheduled), "kind": kind,
        "recipe_id": recipe_id, "title": title, "leftover_of": leftover_of, "servings": servings, "notes": notes,
    }, None


@app.route("/api/meals")
@login_required
def get_meals():
    start = _parse_iso_date(request.args.get("start"))
    if start is None:
        return jsonify({"error": "start must be YYYY-MM-DD"}), 400
    days = request.args.get("days", default=7, type=int)
    if not 1 <= days <= MAX_MEAL_RANGE_DAYS:
        return jsonify({"error": f"days must be 1-{MAX_MEAL_RANGE_DAYS}"}), 400
    end = start + timedelta(days=days - 1)
    rows = _fetch_meals(get_db(), start=start.isoformat(), end=end.isoformat())
    return jsonify([dict(r) for r in rows])


@app.route("/api/meals", methods=["POST"])
@login_required
def create_meal():
    db = get_db()
    fields, error = _validate_meal(db, request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    cur = db.execute(
        "INSERT INTO planned_meals (date, unscheduled, kind, recipe_id, title, leftover_of, servings, notes, "
        "sort, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
        "(SELECT COALESCE(MAX(sort), -1) + 1 FROM planned_meals WHERE date = ?), ?)",
        (fields["date"], fields["unscheduled"], fields["kind"], fields["recipe_id"], fields["title"],
         fields["leftover_of"], fields["servings"], fields["notes"], fields["date"], session["user_id"]),
    )
    db.commit()
    broadcast("update")
    return jsonify(dict(_fetch_meals(db, ids=[cur.lastrowid])[0])), 201


@app.route("/api/meals/<int:meal_id>", methods=["PUT"])
@login_required
def update_meal(meal_id):
    db = get_db()
    existing = db.execute("SELECT id, date FROM planned_meals WHERE id = ?", (meal_id,)).fetchone()
    if existing is None:
        return jsonify({"error": "Meal not found"}), 404
    fields, error = _validate_meal(db, request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    if fields["leftover_of"] == meal_id:
        return jsonify({"error": "Leftovers must point at a cook night"}), 400
    if fields["unscheduled"] and db.execute(
        "SELECT 1 FROM planned_meals WHERE leftover_of = ?", (meal_id,)
    ).fetchone():
        return jsonify({"error": "Leftovers are planned from this night — keep it on a day"}), 400
    # Updated in place (not deleted and re-added) so leftovers that point at
    # this cook night keep pointing at it.
    db.execute(
        "UPDATE planned_meals SET date = ?, unscheduled = ?, kind = ?, recipe_id = ?, title = ?, "
        "leftover_of = ?, servings = ?, notes = ?, updated_at = datetime('now'), "
        "sort = CASE WHEN date = ? THEN sort "
        "ELSE (SELECT COALESCE(MAX(sort), -1) + 1 FROM planned_meals WHERE date = ?) END "
        "WHERE id = ?",
        (fields["date"], fields["unscheduled"], fields["kind"], fields["recipe_id"], fields["title"],
         fields["leftover_of"], fields["servings"], fields["notes"], fields["date"], fields["date"], meal_id),
    )
    if fields["kind"] != "cook":
        db.execute("UPDATE planned_meals SET leftover_of = NULL WHERE leftover_of = ?", (meal_id,))
    db.commit()
    broadcast("update")
    return jsonify(dict(_fetch_meals(db, ids=[meal_id])[0]))


@app.route("/api/meals/<int:meal_id>", methods=["DELETE"])
@login_required
def delete_meal(meal_id):
    db = get_db()
    if db.execute("SELECT id FROM planned_meals WHERE id = ?", (meal_id,)).fetchone() is None:
        return jsonify({"error": "Meal not found"}), 404
    db.execute("DELETE FROM planned_meals WHERE id = ?", (meal_id,))
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Kids' lunches API
# ---------------------------------------------------------------------------
#
# Each kid has at most one lunch per school day: "home" (packed) or "school"
# (bought), with an optional note of what they chose. Dates are household-
# local YYYY-MM-DD, like planned meals. Clearing a day deletes its row.

LUNCH_KINDS = ("home", "school")
MAX_KIDS = 20


def _validate_kid_name(data):
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None, "Name is required"
    name = data["name"].strip()
    if not name:
        return None, "Name is required"
    if len(name) > 50:
        return None, "Name too long"
    return name, None


@app.route("/api/kids")
@login_required
def get_kids():
    rows = get_db().execute("SELECT id, name FROM kids ORDER BY sort, id").fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/kids", methods=["POST"])
@login_required
def create_kid():
    name, error = _validate_kid_name(request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM kids").fetchone()[0] >= MAX_KIDS:
        return jsonify({"error": f"Up to {MAX_KIDS} kids"}), 400
    cur = db.execute(
        "INSERT INTO kids (name, sort) VALUES (?, (SELECT COALESCE(MAX(sort), -1) + 1 FROM kids))", (name,)
    )
    db.commit()
    broadcast("update")
    return jsonify({"id": cur.lastrowid, "name": name}), 201


@app.route("/api/kids/<int:kid_id>", methods=["PUT"])
@login_required
def update_kid(kid_id):
    name, error = _validate_kid_name(request.get_json(silent=True))
    if error:
        return jsonify({"error": error}), 400
    db = get_db()
    if db.execute("UPDATE kids SET name = ? WHERE id = ?", (name, kid_id)).rowcount == 0:
        return jsonify({"error": "Kid not found"}), 404
    db.commit()
    broadcast("update")
    return jsonify({"id": kid_id, "name": name})


@app.route("/api/kids/<int:kid_id>", methods=["DELETE"])
@login_required
def delete_kid(kid_id):
    db = get_db()
    if db.execute("DELETE FROM kids WHERE id = ?", (kid_id,)).rowcount == 0:
        return jsonify({"error": "Kid not found"}), 404
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


@app.route("/api/lunches")
@login_required
def get_lunches():
    start = _parse_iso_date(request.args.get("start"))
    if start is None:
        return jsonify({"error": "start must be YYYY-MM-DD"}), 400
    days = request.args.get("days", default=7, type=int)
    if not 1 <= days <= MAX_MEAL_RANGE_DAYS:
        return jsonify({"error": f"days must be 1-{MAX_MEAL_RANGE_DAYS}"}), 400
    end = start + timedelta(days=days - 1)
    rows = get_db().execute(
        "SELECT kid_id, date, kind, choice FROM kid_lunches WHERE date >= ? AND date <= ? ORDER BY date, kid_id",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/lunches", methods=["PUT"])
@login_required
def set_lunch():
    """Set (or, with kind null, clear) one kid's lunch on one day."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid format"}), 400
    db = get_db()
    kid_id = data.get("kid_id")
    if type(kid_id) is not int or db.execute("SELECT 1 FROM kids WHERE id = ?", (kid_id,)).fetchone() is None:
        return jsonify({"error": "Kid not found"}), 404
    lunch_date = _parse_iso_date(data.get("date"))
    if lunch_date is None:
        return jsonify({"error": "Date must be YYYY-MM-DD"}), 400
    kind = data.get("kind")
    if kind is None:
        db.execute("DELETE FROM kid_lunches WHERE kid_id = ? AND date = ?", (kid_id, lunch_date.isoformat()))
        db.commit()
        broadcast("update")
        return jsonify({"ok": True})
    if kind not in LUNCH_KINDS:
        return jsonify({"error": "Lunch must be home or school"}), 400
    choice = data.get("choice")
    if choice is not None and not isinstance(choice, str):
        return jsonify({"error": "choice must be a string"}), 400
    choice = (choice or "").strip() or None
    if choice and len(choice) > 200:
        return jsonify({"error": "Lunch choice too long"}), 400
    db.execute(
        "INSERT INTO kid_lunches (kid_id, date, kind, choice) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (kid_id, date) DO UPDATE SET kind = excluded.kind, choice = excluded.choice, "
        "updated_at = datetime('now')",
        (kid_id, lunch_date.isoformat(), kind, choice),
    )
    db.commit()
    broadcast("update")
    return jsonify({"kid_id": kid_id, "date": lunch_date.isoformat(), "kind": kind, "choice": choice})


@app.route("/api/lunches/copy-week", methods=["POST"])
@login_required
def copy_lunch_week():
    """Copy one week's lunches onto another, filling only days not yet set."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid format"}), 400
    source = _parse_iso_date(data.get("from"))
    target = _parse_iso_date(data.get("to"))
    if source is None or target is None:
        return jsonify({"error": "from and to must be YYYY-MM-DD"}), 400
    shift = (target - source).days
    if shift == 0:
        return jsonify({"error": "Pick a different week"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT OR IGNORE INTO kid_lunches (kid_id, date, kind, choice) "
        "SELECT kid_id, date(date, ? || ' days'), kind, choice FROM kid_lunches "
        "WHERE date >= ? AND date <= ?",
        (f"{shift:+d}", source.isoformat(), (source + timedelta(days=6)).isoformat()),
    )
    db.commit()
    broadcast("update")
    return jsonify({"copied": cur.rowcount})


# ---------------------------------------------------------------------------
# Adding recipe ingredients to a grocery list
# ---------------------------------------------------------------------------
#
# Two steps so nothing lands on the list unreviewed: preview combines and
# scales the ingredients and compares them with what is already on the list;
# apply writes exactly the lines the user confirmed, in one transaction.

MAX_APPLY_LINES = 200


def _sources_note(sources):
    return f"for {', '.join(sources)}"[:500] if sources else ""


def _list_items_by_key(db, list_id):
    """Map ingredient key -> the list item a new line would merge with.

    An unbought item wins over a bought one: it is still to be purchased, so
    combining with it is the right default. A bought-only match is reported so
    the review can ask whether it's still needed.
    """
    matches = {}
    rows = db.execute(
        "SELECT id, name, quantity, notes, is_bought FROM items WHERE list_id = ? ORDER BY is_bought, id",
        (list_id,),
    ).fetchall()
    for row in rows:
        matches.setdefault(ingr.ingredient_key(row["name"]), row)
    return matches


def _merge_amount(existing_quantity, existing_notes, quantity, note):
    """What a combined item's quantity and notes become.

    Amounts are summed only when both sides read cleanly ("1 lb" + "2 lb");
    otherwise the extra amount goes in the notes, so no typed text is ever
    lost.
    """
    new_quantity = existing_quantity
    extra = ""
    if quantity:
        parsed = ingr.parse_quantity_text(quantity)
        combined = ingr.combine_quantity_text(existing_quantity, parsed) if parsed else None
        if combined and len(combined) <= 50:
            new_quantity = combined
        else:
            extra = f"+{quantity}"
    addition = " ".join(p for p in (extra, note) if p)
    notes = existing_notes or ""
    if addition and addition not in notes:
        notes = f"{notes}; {addition}" if notes else addition
    return new_quantity, (notes[:500] or None)


def _preview_lines(db, list_id, ingredient_lines):
    by_key = _list_items_by_key(db, list_id)
    lines = []
    for agg in ingr.aggregate_ingredients(ingredient_lines):
        existing = by_key.get(agg["key"])
        note = _sources_note(agg["sources"])
        if existing is None:
            status, action = "new", "add"
        elif existing["is_bought"]:
            # Probably bought for this very plan; ask rather than add it again.
            status, action = "bought", "skip"
        else:
            status, action = "on_list", "combine"
        line = {
            "key": agg["key"],
            "name": agg["name"],
            "quantity": agg["quantity_text"][:50] or None,
            "notes": note or None,
            "sources": agg["sources"],
            "unquantified": not agg["quantities"],
            "status": status,
            "action": action,
            "existing_item": None,
        }
        if existing is not None:
            line["existing_item"] = {
                "id": existing["id"], "name": existing["name"], "quantity": existing["quantity"],
                "notes": existing["notes"], "is_bought": bool(existing["is_bought"]),
            }
            if status == "on_list":
                line["combined_quantity"], line["combined_notes"] = _merge_amount(
                    existing["quantity"], existing["notes"], line["quantity"], note)
        lines.append(line)
    return lines


def _int_list(value, max_len):
    if (not isinstance(value, list) or len(value) > max_len
            or any(type(v) is not int for v in value) or len(set(value)) != len(value)):
        return None
    return value


@app.route("/api/shopping/preview", methods=["POST"])
@login_required
def shopping_preview():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid format"}), 400
    db = get_db()
    list_id = data.get("list_id")
    if type(list_id) is not int or db.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone() is None:
        return jsonify({"error": "List not found"}), 404

    ingredient_lines = []
    without_recipe = []
    skipped = []
    if data.get("recipe_id") is not None:
        recipe_id = data["recipe_id"]
        recipe = db.execute(
            "SELECT id, name, servings FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone() if type(recipe_id) is int else None
        if recipe is None:
            return jsonify({"error": "Recipe not found"}), 404
        servings = data.get("servings")
        if not _valid_servings(servings, allow_none=True):
            return jsonify({"error": "Invalid servings"}), 400
        for row in _recipe_ingredients(db, [recipe_id]):
            ingredient_lines.append({
                "name": row["name"], "unit": row["unit"], "source": recipe["name"],
                "qty": ingr.scale_qty(row["qty"], recipe["servings"], servings),
            })
        meals = []
    else:
        meal_ids = _int_list(data.get("meal_ids"), MAX_MEAL_RANGE_DAYS * 4)
        if not meal_ids:
            return jsonify({"error": "Choose at least one meal"}), 400
        meals = _fetch_meals(db, ids=meal_ids)
        if len(meals) != len(meal_ids):
            return jsonify({"error": "Meal not found"}), 404
        recipe_ids = sorted({m["recipe_id"] for m in meals if m["kind"] == "cook" and m["recipe_id"]})
        by_recipe = {}
        for row in _recipe_ingredients(db, recipe_ids):
            by_recipe.setdefault(row["recipe_id"], []).append(row)
        for meal in meals:
            if meal["kind"] != "cook":
                skipped.append(meal["title"])
                continue
            if not meal["recipe_id"]:
                without_recipe.append(meal["title"])
                continue
            for row in by_recipe.get(meal["recipe_id"], []):
                ingredient_lines.append({
                    "name": row["name"], "unit": row["unit"], "source": meal["title"],
                    "qty": ingr.scale_qty(row["qty"], meal["recipe_servings"], meal["servings"]),
                })

    return jsonify({
        "lines": _preview_lines(db, list_id, ingredient_lines),
        "meals_without_recipe": without_recipe,
        "meals_skipped": skipped,
        "already_added": [m["title"] for m in meals
                          if m["added_to_list_at"] and m["kind"] == "cook" and m["recipe_id"]],
    })


@app.route("/api/shopping/apply", methods=["POST"])
@login_required
def shopping_apply():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid format"}), 400
    db = get_db()
    list_id = data.get("list_id")
    if type(list_id) is not int or db.execute("SELECT 1 FROM lists WHERE id = ?", (list_id,)).fetchone() is None:
        return jsonify({"error": "List not found"}), 404
    section = data.get("section", "now")
    if section not in ("now", "later"):
        return jsonify({"error": "Section must be 'now' or 'later'"}), 400
    meal_ids = _int_list(data.get("meal_ids", []), MAX_MEAL_RANGE_DAYS * 4)
    if meal_ids is None:
        return jsonify({"error": "Invalid meals"}), 400
    if meal_ids and len(_fetch_meals(db, ids=meal_ids)) != len(meal_ids):
        return jsonify({"error": "Meal not found"}), 404
    lines = data.get("lines")
    if not isinstance(lines, list) or len(lines) > MAX_APPLY_LINES:
        return jsonify({"error": f"Send between 0 and {MAX_APPLY_LINES} lines"}), 400

    to_add, to_combine = [], []
    for i, line in enumerate(lines):
        if not isinstance(line, dict):
            return jsonify({"error": f"lines[{i}]: Invalid line"}), 400
        action = line.get("action")
        if action == "skip":
            continue
        fields, error = _validate_item({
            "name": line.get("name"), "quantity": line.get("quantity"),
            "notes": line.get("notes"), "section": section,
        })
        if error:
            return jsonify({"error": f"lines[{i}]: {error}"}), 400
        if action == "add":
            to_add.append(fields)
        elif action == "combine":
            item_id = line.get("existing_item_id")
            existing = db.execute(
                "SELECT id, quantity, notes FROM items WHERE id = ? AND list_id = ?", (item_id, list_id)
            ).fetchone() if type(item_id) is int else None
            if existing is None:
                return jsonify({"error": f"lines[{i}]: That item is no longer on the list"}), 400
            to_combine.append((existing, fields))
        else:
            return jsonify({"error": f"lines[{i}]: Action must be add, combine or skip"}), 400

    with db:
        for fields in to_add:
            _insert_item(db, fields, list_id, session["user_id"])
        for existing, fields in to_combine:
            quantity, notes = _merge_amount(existing["quantity"], existing["notes"], fields["quantity"], fields["notes"])
            db.execute(
                "UPDATE items SET quantity = ?, notes = ?, updated_at = datetime('now') WHERE id = ?",
                (quantity, notes, existing["id"]),
            )
        db.execute(
            "UPDATE planned_meals SET added_to_list_id = ?, added_to_list_at = datetime('now') "
            # Only cook nights with a recipe contributed ingredients.
            "WHERE id IN (SELECT value FROM json_each(?)) AND kind = 'cook' AND recipe_id IS NOT NULL",
            (list_id, json.dumps(meal_ids)),
        )
        log_audit("meals.add_to_list", f"Added {len(to_add)}, combined {len(to_combine)} into list {list_id}")
    broadcast("update")
    return jsonify({"added": len(to_add), "combined": len(to_combine)}), 201


# --- Recipe URL import (fetch a page, ask an LLM to extract the recipe) ---
#
# This endpoint never writes to the database -- it only returns extracted
# fields for the client to drop into the create/edit form for review. The
# fetched page is user-supplied and untrusted, so outbound requests are
# restricted to public hosts (see _resolve_safe_ip) to prevent SSRF against
# the server's own network. Every hop resolves the hostname exactly once and
# pins the connection to that specific address (see _PinnedHostAdapter)
# rather than validating a hostname and then letting the HTTP client re-
# resolve and connect separately -- that gap is a DNS-rebinding attack (the
# attacker's DNS returns a public IP for the check and a private one, with a
# very short TTL, for the connection moments later). The LLM's output is
# likewise untrusted text: it is only ever displayed back as editable form
# fields, and is subject to the same length limits as a normal recipe save.

_RECIPE_IMPORT_MAX_REDIRECTS = 3
_RECIPE_IMPORT_MAX_BYTES = 2 * 1024 * 1024
_RECIPE_IMPORT_TIMEOUT = 10
_RECIPE_IMPORT_TEXT_LIMIT = 15000

_RECIPE_EXTRACTION_PROMPT = (
    "You extract recipes from webpage text. Given the page text below, respond with ONLY "
    "a JSON object (no markdown fences, no commentary) shaped like "
    '{"name": "...", "notes": "...", "servings": 4, "ingredients": ["...", ...], "steps": ["...", ...]}. '
    "\"notes\" may be an empty string; it should hold any brief context about the dish, not "
    "the ingredients or steps. \"servings\" is the number of servings the recipe makes, or "
    "null if the page doesn't say. Write each ingredient as one line exactly as the page lists "
    "it, amount first (\"1 1/2 cups flour, sifted\"). If the page text below does not contain a recipe, respond "
    'with exactly {"error": "not_a_recipe"} and nothing else.\n\nPage text:\n'
)


def _parse_recipe_url(url):
    """Validate URL shape (scheme/hostname/port). Returns the parsed URL, or None."""
    try:
        parts = urlparse(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    if port not in (None, 80, 443):
        return None
    return parts


def _resolve_safe_ip(hostname):
    """Resolve `hostname` to the single IP its connection should be pinned to,
    or None if it's unresolvable or unsafe (private/loopback/link-local/etc).

    Only the one address returned needs checking here: because the caller
    pins the actual connection to exactly this address (see
    _PinnedHostAdapter) rather than letting the HTTP client re-resolve later,
    there's no other address the request could end up reaching.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return None
    if not infos:
        return None
    ip = infos[0][4][0]
    addr = ipaddress.ip_address(ip)
    # Judge an IPv4-mapped IPv6 address (::ffff:127.0.0.1) by the IPv4
    # address it carries; older Pythons don't do this inside is_global.
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    # is_global also rejects shared address space such as 100.64.0.0/10,
    # which may expose carrier-grade NAT or overlay-network services.
    if not addr.is_global or addr.is_multicast or addr.is_reserved:
        return None
    return ip


def _pinned_url(parts, ip):
    """The URL actually requested: the vetted IP in place of the hostname.

    Requesting the address itself, rather than handing the user's URL to the
    HTTP client, means the destination is the checked IP by construction.
    The real hostname still goes in the Host header and is used for TLS
    (SNI and certificate checks) by _PinnedHostAdapter.
    """
    host = f"[{ip}]" if ":" in ip else ip
    netloc = host if parts.port is None else f"{host}:{parts.port}"
    return urlunparse((parts.scheme, netloc, parts.path or "/", parts.params, parts.query, ""))


def _is_safe_url(url):
    """Cheap up-front check used to fail fast on an obviously bad URL."""
    parts = _parse_recipe_url(url)
    return parts is not None and _resolve_safe_ip(parts.hostname) is not None


class _PinnedHostAdapter(HTTPAdapter):
    """A requests HTTPAdapter that connects to a pre-validated IP address
    instead of letting urllib3 perform its own, separate DNS lookup -- this
    is what actually closes the DNS-rebinding gap: _resolve_safe_ip's check
    and the connection use the same resolved address.

    TLS verification (SNI + certificate hostname check) still uses the real
    hostname via server_hostname/assert_hostname, so HTTPS sites verify
    normally against the pinned IP.

    Built fresh per request rather than mutating shared/global state (no
    monkeypatching socket.getaddrinfo or similar), so it's safe to use under
    this app's gevent concurrency model (a single worker running many
    cooperatively-scheduled greenlets -- see docker-compose.yml).
    """

    def __init__(self, pinned_ip, hostname):
        self._pinned_ip = pinned_ip
        self._hostname = hostname
        super().__init__()

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
        host_params["host"] = self._pinned_ip
        pool_kwargs["assert_hostname"] = self._hostname
        pool_kwargs["server_hostname"] = self._hostname
        return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)


class _PageTextExtractor(HTMLParser):
    """Collects visible text from an HTML document, skipping script/style."""

    _SKIPPED_TAGS = ("script", "style", "noscript")

    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIPPED_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.chunks.append(text)


def _fetch_recipe_page(url):
    """Fetch `url`, following redirects manually so every hop is re-resolved,
    re-validated, and pinned to the specific address that was checked."""
    for _ in range(_RECIPE_IMPORT_MAX_REDIRECTS + 1):
        parts = _parse_recipe_url(url)
        if parts is None:
            raise ValueError("blocked_url")
        safe_ip = _resolve_safe_ip(parts.hostname)
        if safe_ip is None:
            raise ValueError("blocked_url")

        session = requests.Session()
        # No proxy, .netrc or CA-bundle settings from the environment: the
        # request must go straight to the pinned address.
        session.trust_env = False
        adapter = _PinnedHostAdapter(safe_ip, parts.hostname)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        try:
            resp = session.get(
                _pinned_url(parts, safe_ip),
                timeout=_RECIPE_IMPORT_TIMEOUT,
                stream=True,
                allow_redirects=False,
                # The URL names safe_ip, which would otherwise become the Host
                # header -- send the real hostname so name-based virtual hosting
                # still works.
                headers={"User-Agent": "GroceryListRecipeImport/1.0", "Host": parts.hostname},
            )
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise ValueError("bad_redirect")
                    url = urljoin(url, location)
                    continue
                if resp.status_code != 200:
                    raise ValueError("fetch_failed")
                if "text/html" not in resp.headers.get("Content-Type", ""):
                    raise ValueError("not_html")
                chunks = []
                total = 0
                for chunk in resp.iter_content(chunk_size=8192):
                    total += len(chunk)
                    if total > _RECIPE_IMPORT_MAX_BYTES:
                        raise ValueError("too_large")
                    chunks.append(chunk)
                return b"".join(chunks).decode(resp.encoding or "utf-8", errors="ignore")
            finally:
                resp.close()
        finally:
            session.close()
    raise ValueError("too_many_redirects")


def _html_to_text(page_html):
    parser = _PageTextExtractor()
    parser.feed(page_html)
    return "\n".join(parser.chunks)[:_RECIPE_IMPORT_TEXT_LIMIT]


def _extract_recipe_with_llm(page_text):
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": _RECIPE_EXTRACTION_PROMPT + page_text}],
    )
    raw = "".join(block.text for block in response.content if block.type == "text").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("bad_llm_output")
    if not isinstance(data, dict):
        raise ValueError("bad_llm_output")
    if data.get("error") == "not_a_recipe":
        raise ValueError("not_a_recipe")
    return data


@app.route("/api/recipes/extract-url", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def extract_recipe_url():
    if not config.ANTHROPIC_API_KEY:
        return jsonify({"error": "Recipe URL import is not configured on this server"}), 501

    data = request.get_json() or {}
    url = (data.get("url") or "").strip()
    if not url or len(url) > 2000 or not _is_safe_url(url):
        return jsonify({"error": "Enter a valid, public recipe URL"}), 400

    try:
        page_html = _fetch_recipe_page(url)
    except ValueError:
        return jsonify({"error": "Could not fetch that page"}), 502
    except requests.RequestException:
        return jsonify({"error": "Could not fetch that page"}), 502

    page_text = _html_to_text(page_html)
    if not page_text.strip():
        return jsonify({"error": "Could not find a recipe on that page"}), 422

    try:
        extracted = _extract_recipe_with_llm(page_text)
    except ValueError as e:
        if str(e) == "not_a_recipe":
            return jsonify({"error": "Could not find a recipe on that page"}), 422
        return jsonify({"error": "Could not read the extracted recipe"}), 422
    except anthropic.AnthropicError:
        return jsonify({"error": "Recipe extraction failed"}), 502

    name = str(extracted.get("name") or "")[:200]
    notes = str(extracted.get("notes") or "")[:2000]
    raw_ingredients = extracted.get("ingredients")
    raw_steps = extracted.get("steps")
    ingredients = [str(i)[:200] for i in raw_ingredients if str(i).strip()][:200] \
        if isinstance(raw_ingredients, list) else []
    steps = [str(s)[:300] for s in raw_steps if str(s).strip()][:100] \
        if isinstance(raw_steps, list) else []

    if not name or not ingredients:
        return jsonify({"error": "Could not find a recipe on that page"}), 422

    servings = extracted.get("servings")
    if not _valid_servings(servings):
        servings = None
    return jsonify({
        "name": name, "notes": notes, "servings": servings, "source_url": url,
        # Split into editor fields here so the review form shows amounts and
        # units the user can correct before saving.
        "ingredients": [ingr.parse_ingredient_line(i) for i in ingredients][:MAX_RECIPE_INGREDIENTS],
        "steps": steps,
    })


# ---------------------------------------------------------------------------
# Change password (forced after admin creates/resets)
# ---------------------------------------------------------------------------

@app.route("/change-password", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
@login_required
def change_password():
    db = get_db()
    user = db.execute(
        "SELECT password_hash, must_change_password, session_epoch FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    # Forced resets are exempt: the admin just set a temporary password the
    # user authenticated with, so re-prompting for it proves nothing.
    require_current = not user["must_change_password"]

    if request.method == "GET":
        return render_template("change_password.html", require_current=require_current)

    current = request.form.get("current", "").strip()
    new_password = request.form.get("password", "").strip()
    confirm = request.form.get("confirm", "").strip()

    if not new_password:
        flash("Password is required.")
        return render_template("change_password.html", require_current=require_current), 400
    if len(new_password) < 5:
        flash("Password must be at least 5 characters.")
        return render_template("change_password.html", require_current=require_current), 400
    if new_password != confirm:
        flash("Passwords do not match.")
        return render_template("change_password.html", require_current=require_current), 400
    if require_current and not check_password(current, user["password_hash"].encode()):
        flash("Current password is incorrect.")
        return render_template("change_password.html", require_current=require_current), 403

    pw_hash = hash_password(new_password)
    # Bump the epoch so cookies issued before the change stop authenticating.
    # Changing your password is the standard reflex after a suspected
    # compromise, so it has to actually evict whoever else is holding a session.
    new_epoch = user["session_epoch"] + 1
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0, "
        "session_epoch = ? WHERE id = ?",
        (pw_hash, new_epoch, session["user_id"]),
    )
    db.commit()
    session["epoch"] = new_epoch          # keep the device that made the change
    session["must_change_password"] = False
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.route("/admin/users")
@admin_required
def admin_users():
    db = get_db()
    users = db.execute("SELECT id, username, role, is_active, must_change_password FROM users ORDER BY id").fetchall()
    logs = db.execute(
        "SELECT timestamp, actor_username, action, detail FROM audit_log ORDER BY id DESC LIMIT 200"
    ).fetchall()
    return render_template("admin.html", users=users, logs=logs)


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not password:
        flash("Username and password are required.")
        return redirect(url_for("admin_users"))
    if len(password) < 5:
        flash("Password must be at least 5 characters.")
        return redirect(url_for("admin_users"))

    db = get_db()
    if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        flash("Username already taken.")
        return redirect(url_for("admin_users"))

    pw_hash = hash_password(password)
    db.execute(
        "INSERT INTO users (username, password_hash, must_change_password) VALUES (?, ?, 1)",
        (username, pw_hash),
    )
    log_audit("admin.user_create", f"Created user '{username}'")
    db.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/disable", methods=["POST"])
@admin_required
def admin_disable_user(user_id):
    if user_id == session["user_id"]:
        flash("You cannot disable your own account.")
        return redirect(url_for("admin_users"))
    db = get_db()
    target = db.execute("SELECT username, is_active FROM users WHERE id = ?", (user_id,)).fetchone()
    db.execute(
        "UPDATE users SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END, "
        "session_epoch = session_epoch + CASE WHEN is_active = 1 THEN 1 ELSE 0 END "
        "WHERE id = ?", (user_id,)
    )
    if target:
        verb = "Disabled" if target["is_active"] == 1 else "Enabled"
        log_audit("admin.user_toggle", f"{verb} user '{target['username']}'")
    db.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["GET", "POST"])
@admin_required
def admin_reset_password(user_id):
    db = get_db()
    user = db.execute(
        "SELECT id, username, session_epoch FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not user:
        return redirect(url_for("admin_users"))

    if request.method == "GET":
        return render_template("admin_reset_password.html", user=user)

    new_password = request.form.get("password", "").strip()
    if not new_password:
        flash("Password is required.")
        return render_template("admin_reset_password.html", user=user), 400
    if len(new_password) < 5:
        flash("Password must be at least 5 characters.")
        return render_template("admin_reset_password.html", user=user), 400

    pw_hash = hash_password(new_password)
    # Bump the epoch too. A reset is the remediation for a compromised account,
    # so it must revoke the target's existing cookies -- otherwise the intruder
    # keeps their session *and* inherits the must_change_password exemption in
    # change_password(), letting them set a password of their own choosing.
    new_epoch = user["session_epoch"] + 1
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1, "
        "session_epoch = ? WHERE id = ?",
        (pw_hash, new_epoch, user_id),
    )
    log_audit("admin.password_reset", f"Reset password for user '{user['username']}'")
    db.commit()
    if user_id == session["user_id"]:
        # Self-reset: keep the session that issued it, as revoke-sessions does.
        session["epoch"] = new_epoch
        session["must_change_password"] = True
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/revoke-sessions", methods=["POST"])
@admin_required
def admin_revoke_sessions(user_id):
    db = get_db()
    target = db.execute("SELECT username, session_epoch FROM users WHERE id = ?", (user_id,)).fetchone()
    if not target:
        flash("User not found.")
        return redirect(url_for("admin_users"))
    new_epoch = target["session_epoch"] + 1
    db.execute("UPDATE users SET session_epoch = ? WHERE id = ?", (new_epoch, user_id))
    log_audit("admin.sessions_revoke", f"Revoked all sessions for user '{target['username']}'")
    db.commit()
    if user_id == session["user_id"]:
        # Keep the session that issued the revocation; every other device dies.
        session["epoch"] = new_epoch
        flash("All your other sessions have been logged out.")
    else:
        flash(f"All sessions for '{target['username']}' have been logged out.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    if user_id == session["user_id"]:
        flash("You cannot delete your own account.")
        return redirect(url_for("admin_users"))
    db = get_db()
    target = db.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    db.execute("UPDATE items SET added_by = NULL WHERE added_by = ?", (user_id,))
    db.execute("UPDATE lists SET created_by = NULL WHERE created_by = ?", (user_id,))
    # Upgraded databases retain the original recipes table definition, so
    # clear ownership explicitly as well as using ON DELETE SET NULL on new DBs.
    db.execute("UPDATE recipes SET created_by = NULL WHERE created_by = ?", (user_id,))
    db.execute("UPDATE audit_log SET actor_id = NULL WHERE actor_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if target:
        log_audit("admin.user_delete", f"Deleted user '{target['username']}'")
    db.commit()
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
# PWA manifest
# ---------------------------------------------------------------------------

@functools.cache
def pwa_asset_version():
    # Every file under /static/ is cached cache-first by the service worker, so
    # all of them (except the worker itself) must feed the cache key.
    digest = hashlib.sha256()
    static_root = Path(app.static_folder)
    for path in sorted(p for p in static_root.rglob("*") if p.is_file()):
        if path.name == "sw.js":
            continue
        digest.update(path.relative_to(static_root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


@app.route("/manifest.webmanifest")
def manifest():
    return app.send_static_file("manifest.webmanifest"), 200, {"Content-Type": "application/manifest+json"}


@app.route("/sw.js")
def service_worker():
    source = (Path(app.static_folder) / "sw.js").read_text()
    source = source.replace("__ASSET_VERSION__", pwa_asset_version())
    response = Response(source, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

@app.route("/api/stream")
@login_required
def stream():
    q = queue.Queue(maxsize=50)
    with subscribers_lock:
        subscribers.append(q)

    def event_stream():
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            with subscribers_lock:
                if q in subscribers:
                    subscribers.remove(q)

    return Response(event_stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/healthz")
@limiter.exempt
def healthz():
    # Liveness for the container health check. Independent of setup state and
    # authentication, so a fresh install without users still reports healthy.
    try:
        get_db().execute("SELECT 1 FROM users LIMIT 1").fetchall()
    except sqlite3.Error:
        app.logger.exception("Health check database query failed")
        return Response("unavailable\n", status=503, mimetype="text/plain")
    return Response("ok\n", mimetype="text/plain", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

with app.app_context():
    init_db()
    upgrade_db()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, threaded=True)
