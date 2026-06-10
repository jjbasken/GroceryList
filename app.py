import json
import queue
import sqlite3
import threading
import time
from datetime import timedelta
from functools import wraps

import bcrypt
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
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import config

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
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

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        # Re-validate is_active on every request (cheap indexed lookup)
        user = get_db().execute(
            "SELECT is_active FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
        if not user or user["is_active"] != 1:
            session.clear()
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = get_db().execute(
            "SELECT is_active, role FROM users WHERE id = ?", (session["user_id"],)
        ).fetchone()
        if not user or user["is_active"] != 1:
            session.clear()
            return redirect(url_for("login"))
        if user["role"] != "admin":
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


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
    user = db.execute("SELECT id, role FROM users WHERE username = ?", (username,)).fetchone()
    session["user_id"] = user["id"]
    session["username"] = username
    session["role"] = user["role"]
    session["must_change_password"] = False
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
    if session.get("must_change_password"):
        return redirect(url_for("change_password"))
    return render_template("list.html", username=session["username"],
                           is_admin=(session.get("role") == "admin"))


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.route("/api/lists")
@login_required
def get_lists():
    db = get_db()
    rows = db.execute("SELECT id, name FROM lists ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


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
    count = db.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
    if count <= 1:
        return jsonify({"error": "Cannot delete the last list"}), 400
    lst = db.execute("SELECT name FROM lists WHERE id = ?", (list_id,)).fetchone()
    db.execute("DELETE FROM items WHERE list_id = ?", (list_id,))
    db.execute("DELETE FROM lists WHERE id = ?", (list_id,))
    log_audit("list.delete", f"Deleted list '{lst['name'] if lst else list_id}'")
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
# Change password (forced after admin creates/resets)
# ---------------------------------------------------------------------------

@app.route("/change-password", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
@login_required
def change_password():
    db = get_db()
    user = db.execute(
        "SELECT password_hash, must_change_password FROM users WHERE id = ?",
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
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
        (pw_hash, session["user_id"]),
    )
    db.commit()
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
    user = db.execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone()
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
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
        (pw_hash, user_id),
    )
    log_audit("admin.password_reset", f"Reset password for user '{user['username']}'")
    db.commit()
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
