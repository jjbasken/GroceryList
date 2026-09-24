"""
Pytest configuration.

Environment variables must be set *before* app.py is imported so that
config.py picks them up at module load time.
"""
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE"] = _tmp.name

import bcrypt  # noqa: E402
import pytest  # noqa: E402

import config as _config  # noqa: E402

# Redirect the module-level attribute so every get_db() call uses our temp file.
_config.DATABASE = _tmp.name

from app import app as _flask_app, get_db, limiter as _limiter  # noqa: E402

# Apply test overrides at import time so CSRF checks and cookie handling
# see the correct values from the very first request.
_flask_app.config.update(
    TESTING=True,
    WTF_CSRF_ENABLED=False,
    SESSION_COOKIE_SECURE=False,
    RATELIMIT_ENABLED=False,
)
# flask-limiter caches `enabled` as an instance attribute during init_app,
# so the config key above is not enough — set the attribute directly.
_limiter.enabled = False


# ---------------------------------------------------------------------------
# Core fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app():
    yield _flask_app


@pytest.fixture(autouse=True)
def _clean_db(app):
    """Wipe every table before each test for full isolation."""
    with app.app_context():
        db = get_db()
        for table in (
            "audit_log", "item_name_history", "recipe_ingredients", "recipes",
            "items", "lists", "users",
        ):
            db.execute(f"DELETE FROM {table}")  # noqa: S608
        db.commit()


@pytest.fixture
def client(app):
    with app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Database helpers (importable by test modules)
# ---------------------------------------------------------------------------


def make_user(
    username="alice",
    password="pass1234",
    role="user",
    is_active=1,
    must_change=0,
):
    with _flask_app.app_context():
        db = get_db()
        pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        cur = db.execute(
            "INSERT INTO users (username, password_hash, role, is_active, must_change_password)"
            " VALUES (?, ?, ?, ?, ?)",
            (username, pw, role, is_active, must_change),
        )
        db.commit()
        return cur.lastrowid


def make_list(name="Groceries", created_by=None):
    with _flask_app.app_context():
        db = get_db()
        cur = db.execute(
            "INSERT INTO lists (name, created_by) VALUES (?, ?)", (name, created_by)
        )
        db.commit()
        return cur.lastrowid


def make_item(list_id, name="Milk", section="now", is_bought=0, added_by=None):
    with _flask_app.app_context():
        db = get_db()
        cur = db.execute(
            "INSERT INTO items (name, section, is_bought, list_id, added_by)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, section, is_bought, list_id, added_by),
        )
        db.commit()
        return cur.lastrowid


def make_recipe(name="Pancakes", notes=None, steps=None, ingredients=("Flour", "Eggs"), created_by=None):
    with _flask_app.app_context():
        db = get_db()
        steps_text = "\n".join(steps) if steps else None
        cur = db.execute(
            "INSERT INTO recipes (name, notes, steps, created_by) VALUES (?, ?, ?, ?)",
            (name, notes, steps_text, created_by),
        )
        recipe_id = cur.lastrowid
        db.executemany(
            "INSERT INTO recipe_ingredients (recipe_id, text) VALUES (?, ?)",
            [(recipe_id, text) for text in ingredients],
        )
        db.commit()
        return recipe_id


def do_login(client, username="alice", password="pass1234"):
    response = client.post("/login", data={"username": username, "password": password})
    # Model the account-bound header sent by the rendered app page.
    with client.session_transaction() as session:
        if "account_id" in session:
            client.environ_base["HTTP_X_ACCOUNT_ID"] = session["account_id"]
    return response
