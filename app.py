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

import config

app = Flask(__name__)
app.secret_key = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(days=36500)

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


def init_db():
    db = sqlite3.connect(config.DATABASE)
    with open("schema.sql") as f:
        db.executescript(f.read())
    db.close()


def upgrade_db():
    db = sqlite3.connect(config.DATABASE)
    cols = {row[1] for row in db.execute("PRAGMA table_info(users)")}
    if 'role' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    if 'is_active' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if 'must_change_password' not in cols:
        db.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
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
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
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
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
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

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
    db.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
        (username, pw_hash),
    )
    db.commit()

    user = db.execute("SELECT id, role FROM users WHERE username = ?", (username,)).fetchone()
    session["user_id"] = user["id"]
    session["username"] = username
    session["role"] = user["role"]
    session["must_change_password"] = False
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not has_users():
        return redirect(url_for("register"))

    if request.method == "GET":
        return render_template("login.html")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

    if user and bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        if user["is_active"] != 1:
            flash("Your account has been disabled.")
            return render_template("login.html"), 401
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


@app.route("/logout")
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

@app.route("/api/items")
@login_required
def get_items():
    db = get_db()
    rows = db.execute(
        "SELECT items.*, users.username AS added_by_name "
        "FROM items JOIN users ON items.added_by = users.id "
        "ORDER BY items.is_bought ASC, items.created_at DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/items", methods=["POST"])
@login_required
def add_item():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    section = data.get("section", "now")

    if not name:
        return jsonify({"error": "Name is required"}), 400
    if section not in ("now", "later"):
        return jsonify({"error": "Section must be 'now' or 'later'"}), 400

    db = get_db()
    cur = db.execute(
        "INSERT INTO items (name, section, added_by) VALUES (?, ?, ?)",
        (name, section, session["user_id"]),
    )
    db.commit()

    item = db.execute(
        "SELECT items.*, users.username AS added_by_name "
        "FROM items JOIN users ON items.added_by = users.id "
        "WHERE items.id = ?",
        (cur.lastrowid,),
    ).fetchone()

    broadcast("update")
    return jsonify(dict(item)), 201


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
    db.execute("DELETE FROM items WHERE id = ?", (item_id,))
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


@app.route("/api/items/clear-bought", methods=["POST"])
@login_required
def clear_bought():
    db = get_db()
    db.execute("DELETE FROM items WHERE is_bought = 1")
    db.commit()
    broadcast("update")
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Change password (forced after admin creates/resets)
# ---------------------------------------------------------------------------

@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return render_template("change_password.html")

    new_password = request.form.get("password", "").strip()
    confirm = request.form.get("confirm", "").strip()

    if not new_password:
        flash("Password is required.")
        return render_template("change_password.html"), 400
    if new_password != confirm:
        flash("Passwords do not match.")
        return render_template("change_password.html"), 400

    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    db = get_db()
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
    return render_template("admin.html", users=users)


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()

    if not username or not password:
        flash("Username and password are required.")
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
    db.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/disable", methods=["POST"])
@admin_required
def admin_disable_user(user_id):
    if user_id == session["user_id"]:
        flash("You cannot disable your own account.")
        return redirect(url_for("admin_users"))
    db = get_db()
    db.execute("UPDATE users SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?", (user_id,))
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

    pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
        (pw_hash, user_id),
    )
    db.commit()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    if user_id == session["user_id"]:
        flash("You cannot delete your own account.")
        return redirect(url_for("admin_users"))
    db = get_db()
    db.execute("UPDATE items SET added_by = NULL WHERE added_by = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return redirect(url_for("admin_users"))


# ---------------------------------------------------------------------------
# PWA manifest
# ---------------------------------------------------------------------------

@app.route("/manifest.json")
def manifest():
    return app.send_static_file("manifest.json"), 200, {"Content-Type": "application/manifest+json"}


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
