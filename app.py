import ipaddress
import json
import queue
import re
import socket
import sqlite3
import threading
import time
from datetime import timedelta
from functools import wraps
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

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

import config

app = Flask(__name__)
app.config.from_object(config)  # SECRET_KEY, WTF_CSRF_TIME_LIMIT
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


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

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

    # --- Recipes tables ---
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'recipes' not in tables:
        db.execute("""
            CREATE TABLE recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                notes TEXT,
                steps TEXT,
                created_by INTEGER REFERENCES users(id),
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
    if 'recipe_ingredients' not in tables:
        db.execute("""
            CREATE TABLE recipe_ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
                text TEXT NOT NULL
            )
        """)

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

    if request.method == "GET":
        return render_template("register.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not password:
        flash("Username and password are required.")
        return render_template("register.html"), 400
    if len(password) < 5:
        flash("Password must be at least 5 characters.")
        return render_template("register.html"), 400

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
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
    password_ok = bcrypt.checkpw(password.encode(), candidate_hash)

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
@login_required
def recipes():
    return render_template("recipes.html", username=session["username"],
                           is_admin=(session.get("role") == "admin"),
                           import_url_enabled=bool(config.ANTHROPIC_API_KEY))


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
    data = request.get_json()
    name = (data.get("name") or "").strip()
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
    broadcast("update")
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


@app.route("/api/items", methods=["POST"])
@login_required
def add_item():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    section = data.get("section", "now")
    quantity = (data.get("quantity") or "").strip() or None
    notes = (data.get("notes") or "").strip() or None
    list_id = data.get("list_id")

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if len(name) > 200:
        return jsonify({"error": "Name too long"}), 400
    if quantity and len(quantity) > 50:
        return jsonify({"error": "Quantity too long"}), 400
    if notes and len(notes) > 500:
        return jsonify({"error": "Notes too long"}), 400
    if section not in ("now", "later"):
        return jsonify({"error": "Section must be 'now' or 'later'"}), 400

    db = get_db()
    if list_id is None:
        first = db.execute("SELECT id FROM lists ORDER BY id LIMIT 1").fetchone()
        list_id = first["id"] if first else None

    cur = db.execute(
        "INSERT INTO items (name, section, quantity, notes, list_id, added_by) VALUES (?, ?, ?, ?, ?, ?)",
        (name, section, quantity, notes, list_id, session["user_id"]),
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
        (cur.lastrowid,),
    ).fetchone()

    broadcast("update")
    return jsonify(dict(item)), 201


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
    data = request.get_json()
    name = (data.get("name") or "").strip()
    section = data.get("section", "now")
    quantity = (data.get("quantity") or "").strip() or None
    notes = (data.get("notes") or "").strip() or None

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if len(name) > 200:
        return jsonify({"error": "Name too long"}), 400
    if quantity and len(quantity) > 50:
        return jsonify({"error": "Quantity too long"}), 400
    if notes and len(notes) > 500:
        return jsonify({"error": "Notes too long"}), 400
    if section not in ("now", "later"):
        return jsonify({"error": "Section must be 'now' or 'later'"}), 400

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


@app.route("/api/recipes")
@login_required
def get_recipes():
    db = get_db()
    rows = db.execute(
        "SELECT recipes.*, "
        "(SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id = recipes.id) AS ingredient_count "
        "FROM recipes ORDER BY name COLLATE NOCASE"
    ).fetchall()
    is_admin = current_user_is_admin()
    return jsonify([
        {
            "id": r["id"],
            "name": r["name"],
            "notes": r["notes"],
            "ingredient_count": r["ingredient_count"],
            "created_by": r["created_by"],
            "can_delete": may_delete_recipe(r["created_by"], is_admin),
        }
        for r in rows
    ])


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
    ingredients = db.execute(
        "SELECT id, text FROM recipe_ingredients WHERE recipe_id = ? ORDER BY id",
        (recipe_id,),
    ).fetchall()
    data = dict(recipe)
    data["ingredients"] = [dict(i) for i in ingredients]
    data["can_delete"] = may_delete_recipe(recipe["created_by"], current_user_is_admin())
    return jsonify(data)


def _validate_recipe_payload(data):
    """Shared validation for create/update. Returns (fields_dict, error_response_or_None)."""
    name = (data.get("name") or "").strip()
    notes = (data.get("notes") or "").strip() or None

    if not name:
        return None, (jsonify({"error": "Name is required"}), 400)
    if len(name) > 200:
        return None, (jsonify({"error": "Name too long"}), 400)
    if notes and len(notes) > 2000:
        return None, (jsonify({"error": "Notes too long"}), 400)

    ingredients, err = _prepare_lines(data.get("ingredients"), max_lines=200, max_len=200)
    if err:
        return None, (jsonify({"error": err}), 400)
    if not ingredients:
        return None, (jsonify({"error": "At least one ingredient is required"}), 400)

    steps, err = _prepare_lines(data.get("steps"), max_lines=100, max_len=300)
    if err:
        return None, (jsonify({"error": err}), 400)
    steps_text = "\n".join(steps) or None
    if steps_text and len(steps_text) > 4000:
        return None, (jsonify({"error": "Steps too long"}), 400)

    return {"name": name, "notes": notes, "steps_text": steps_text, "ingredients": ingredients}, None


@app.route("/api/recipes", methods=["POST"])
@login_required
def create_recipe():
    fields, error = _validate_recipe_payload(request.get_json() or {})
    if error:
        return error

    db = get_db()
    cur = db.execute(
        "INSERT INTO recipes (name, notes, steps, created_by) VALUES (?, ?, ?, ?)",
        (fields["name"], fields["notes"], fields["steps_text"], session["user_id"]),
    )
    recipe_id = cur.lastrowid
    db.executemany(
        "INSERT INTO recipe_ingredients (recipe_id, text) VALUES (?, ?)",
        [(recipe_id, text) for text in fields["ingredients"]],
    )
    db.commit()
    broadcast("update")
    return jsonify({"id": recipe_id}), 201


@app.route("/api/recipes/<int:recipe_id>", methods=["PUT"])
@login_required
def update_recipe(recipe_id):
    db = get_db()
    if db.execute("SELECT id FROM recipes WHERE id = ?", (recipe_id,)).fetchone() is None:
        return jsonify({"error": "Recipe not found"}), 404

    fields, error = _validate_recipe_payload(request.get_json() or {})
    if error:
        return error

    db.execute(
        "UPDATE recipes SET name = ?, notes = ?, steps = ?, updated_at = datetime('now') WHERE id = ?",
        (fields["name"], fields["notes"], fields["steps_text"], recipe_id),
    )
    db.execute("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
    db.executemany(
        "INSERT INTO recipe_ingredients (recipe_id, text) VALUES (?, ?)",
        [(recipe_id, text) for text in fields["ingredients"]],
    )
    db.commit()
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
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    log_audit("recipe.delete", f"Deleted recipe '{recipe['name']}'")
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


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
    '{"name": "...", "notes": "...", "ingredients": ["...", ...], "steps": ["...", ...]}. '
    "\"notes\" may be an empty string; it should hold any brief context about the dish, not "
    "the ingredients or steps. If the page text below does not contain a recipe, respond "
    'with exactly {"error": "not_a_recipe"} and nothing else.\n\nPage text:\n'
)


def _parse_recipe_url(url):
    """Validate URL shape (scheme/hostname/port). Returns the parsed URL, or None."""
    parts = urlparse(url)
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    if parts.port not in (None, 80, 443):
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
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        return None
    return ip


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
        adapter = _PinnedHostAdapter(safe_ip, parts.hostname)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        try:
            resp = session.get(
                url,
                timeout=_RECIPE_IMPORT_TIMEOUT,
                stream=True,
                allow_redirects=False,
                # The connection is pinned to safe_ip (see _PinnedHostAdapter), which
                # would otherwise become the default Host header -- send the real
                # hostname explicitly so name-based virtual hosting still works.
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

    return jsonify({"name": name, "notes": notes, "ingredients": ingredients, "steps": steps})


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
    if require_current and not bcrypt.checkpw(current.encode(), user["password_hash"].encode()):
        flash("Current password is incorrect.")
        return render_template("change_password.html", require_current=require_current), 403

    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
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

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
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
    db.execute("UPDATE users SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?", (user_id,))
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

    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
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
    db.execute("UPDATE audit_log SET actor_id = NULL WHERE actor_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if target:
        log_audit("admin.user_delete", f"Deleted user '{target['username']}'")
    db.commit()
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
# PWA manifest
# ---------------------------------------------------------------------------

@app.route("/manifest.webmanifest")
def manifest():
    return app.send_static_file("manifest.webmanifest"), 200, {"Content-Type": "application/manifest+json"}


@app.route("/sw.js")
def service_worker():
    response = app.send_static_file("sw.js")
    response.headers["Cache-Control"] = "no-cache"
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
# Startup
# ---------------------------------------------------------------------------

with app.app_context():
    init_db()
    upgrade_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)
