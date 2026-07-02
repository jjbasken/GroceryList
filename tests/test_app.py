"""
Regression tests for the GroceryList Flask app.

Covers: auth, lists API, items API, item history, change-password,
        admin user management, security headers, auth guards, and PWA endpoints.
"""
import json

import pytest

from app import app as flask_app, get_db
from conftest import do_login, make_item, make_list, make_user


# ---------------------------------------------------------------------------
# Tiny request helpers
# ---------------------------------------------------------------------------


def jpost(client, url, payload):
    return client.post(
        url, data=json.dumps(payload), content_type="application/json"
    )


def db_query(app, sql, params=()):
    with app.app_context():
        return get_db().execute(sql, params).fetchone()


def db_count(app, sql, params=()):
    with app.app_context():
        return get_db().execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegister:
    def test_get_shows_form_when_no_users(self, client):
        rv = client.get("/register")
        assert rv.status_code == 200

    def test_redirects_to_login_when_users_exist(self, client):
        make_user()
        rv = client.get("/register")
        assert rv.status_code == 302
        assert "/login" in rv.location

    def test_first_user_becomes_admin(self, client, app):
        rv = client.post("/register", data={"username": "alice", "password": "pass1234"})
        assert rv.status_code == 302
        row = db_query(app, "SELECT role FROM users WHERE username = 'alice'")
        assert row["role"] == "admin"

    def test_register_creates_session(self, client):
        client.post("/register", data={"username": "alice", "password": "pass1234"})
        rv = client.get("/")
        assert rv.status_code == 200  # logged in, not redirected

    def test_empty_fields_rejected(self, client):
        rv = client.post("/register", data={"username": "", "password": ""})
        assert rv.status_code == 400

    def test_short_password_rejected(self, client):
        rv = client.post("/register", data={"username": "alice", "password": "ab"})
        assert rv.status_code == 400


# ---------------------------------------------------------------------------
# Login / Logout
# ---------------------------------------------------------------------------


class TestLogin:
    def test_get_redirects_to_register_when_no_users(self, client):
        rv = client.get("/login")
        assert rv.status_code == 302
        assert "/register" in rv.location

    def test_get_shows_form_when_users_exist(self, client):
        make_user()
        rv = client.get("/login")
        assert rv.status_code == 200

    def test_valid_credentials_redirect_to_index(self, client):
        make_user()
        rv = do_login(client)
        assert rv.status_code == 302
        assert "/login" not in rv.location

    def test_wrong_password_returns_401(self, client):
        make_user()
        rv = do_login(client, password="wrongpass")
        assert rv.status_code == 401

    def test_unknown_username_returns_401(self, client):
        make_user()
        rv = do_login(client, username="nobody")
        assert rv.status_code == 401

    def test_disabled_account_returns_401(self, client):
        make_user(is_active=0)
        rv = do_login(client)
        assert rv.status_code == 401

    def test_must_change_password_redirects_to_change_password(self, client):
        make_user(must_change=1)
        rv = do_login(client)
        assert rv.status_code == 302
        assert "/change-password" in rv.location

    def test_inactive_user_cant_reach_index_even_with_valid_session(self, client, app):
        uid = make_user()
        do_login(client)
        # disable the user after they've logged in
        with app.app_context():
            get_db().execute("UPDATE users SET is_active = 0 WHERE id = ?", (uid,))
            get_db().commit()
        rv = client.get("/")
        assert rv.status_code == 302
        assert "/login" in rv.location


class TestLogout:
    def test_logout_ends_session(self, client):
        make_user()
        do_login(client)
        client.post("/logout")
        rv = client.get("/")
        assert rv.status_code == 302
        assert "/login" in rv.location


# ---------------------------------------------------------------------------
# Index / Change password guard
# ---------------------------------------------------------------------------


class TestIndex:
    def test_requires_auth(self, client):
        rv = client.get("/")
        assert rv.status_code == 302
        assert "/login" in rv.location

    def test_authenticated_user_sees_page(self, client):
        make_user()
        do_login(client)
        rv = client.get("/")
        assert rv.status_code == 200

    def test_must_change_password_flag_redirects(self, client):
        make_user()
        do_login(client)
        with client.session_transaction() as sess:
            sess["must_change_password"] = True
        rv = client.get("/")
        assert rv.status_code == 302
        assert "/change-password" in rv.location


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------


class TestChangePassword:
    def test_requires_auth(self, client):
        rv = client.get("/change-password")
        assert rv.status_code == 302

    def test_get_shows_form(self, client):
        make_user()
        do_login(client)
        rv = client.get("/change-password")
        assert rv.status_code == 200

    def test_forced_reset_succeeds_without_current_password(self, client, app):
        make_user(must_change=1)
        do_login(client)
        rv = client.post(
            "/change-password", data={"password": "newpass99", "confirm": "newpass99"}
        )
        assert rv.status_code == 302
        row = db_query(app, "SELECT must_change_password FROM users WHERE username = 'alice'")
        assert row["must_change_password"] == 0

    def test_new_password_works_for_subsequent_login(self, client):
        make_user()
        do_login(client)
        client.post(
            "/change-password",
            data={"current": "pass1234", "password": "newpass99", "confirm": "newpass99"},
        )
        client.post("/logout")
        rv = do_login(client, username="alice", password="newpass99")
        assert rv.status_code == 302

    def test_wrong_current_password_rejected(self, client):
        make_user()
        do_login(client)
        rv = client.post(
            "/change-password",
            data={"current": "wrongpass", "password": "newpass99", "confirm": "newpass99"},
        )
        assert rv.status_code == 403
        # old password still works
        client.post("/logout")
        rv = do_login(client)
        assert rv.status_code == 302

    def test_missing_current_password_rejected(self, client):
        make_user()
        do_login(client)
        rv = client.post(
            "/change-password", data={"password": "newpass99", "confirm": "newpass99"}
        )
        assert rv.status_code == 403

    def test_mismatched_passwords_rejected(self, client):
        make_user()
        do_login(client)
        rv = client.post(
            "/change-password",
            data={"current": "pass1234", "password": "newpass99", "confirm": "different"},
        )
        assert rv.status_code == 400

    def test_short_password_rejected(self, client):
        make_user()
        do_login(client)
        rv = client.post(
            "/change-password", data={"current": "pass1234", "password": "ab", "confirm": "ab"}
        )
        assert rv.status_code == 400


# ---------------------------------------------------------------------------
# Lists API
# ---------------------------------------------------------------------------


class TestListsAPI:
    def test_requires_auth(self, client):
        rv = client.get("/api/lists")
        assert rv.status_code == 401

    def test_get_returns_empty_list(self, client):
        make_user()
        do_login(client)
        assert client.get("/api/lists").get_json() == []

    def test_get_returns_all_lists(self, client):
        make_user()
        make_list("Groceries")
        make_list("Hardware")
        do_login(client)
        names = [d["name"] for d in client.get("/api/lists").get_json()]
        assert "Groceries" in names and "Hardware" in names

    def test_create_list(self, client, app):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/lists", {"name": "Party"})
        assert rv.status_code == 201
        data = rv.get_json()
        assert data["name"] == "Party"
        assert db_query(app, "SELECT name FROM lists WHERE id = ?", (data["id"],))

    def test_create_list_empty_name_rejected(self, client):
        make_user()
        do_login(client)
        assert jpost(client, "/api/lists", {"name": ""}).status_code == 400

    def test_create_list_name_too_long_rejected(self, client):
        make_user()
        do_login(client)
        assert jpost(client, "/api/lists", {"name": "x" * 101}).status_code == 400

    def test_delete_list(self, client):
        make_user()
        lid = make_list("First")
        make_list("Second")
        do_login(client)
        rv = client.delete(f"/api/lists/{lid}")
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True

    def test_cannot_delete_last_list(self, client):
        make_user()
        lid = make_list("Only")
        do_login(client)
        assert client.delete(f"/api/lists/{lid}").status_code == 400

    def test_delete_list_requires_auth(self, client):
        lid = make_list()
        assert client.delete(f"/api/lists/{lid}").status_code == 401


# ---------------------------------------------------------------------------
# Items API
# ---------------------------------------------------------------------------


class TestItemsAPI:
    def test_get_requires_auth(self, client):
        assert client.get("/api/items").status_code == 401

    def test_get_items_empty_for_list(self, client):
        make_user()
        lid = make_list()
        do_login(client)
        assert client.get(f"/api/items?list_id={lid}").get_json() == []

    def test_get_items_scoped_to_list(self, client):
        make_user()
        lid1 = make_list("A")
        lid2 = make_list("B")
        make_item(lid1, "Milk")
        make_item(lid2, "Bread")
        do_login(client)
        items = client.get(f"/api/items?list_id={lid1}").get_json()
        assert len(items) == 1
        assert items[0]["name"] == "Milk"

    def test_get_items_defaults_to_first_list(self, client):
        make_user()
        lid = make_list()
        make_item(lid, "DefaultItem")
        do_login(client)
        names = [i["name"] for i in client.get("/api/items").get_json()]
        assert "DefaultItem" in names

    def test_add_item(self, client):
        make_user()
        lid = make_list()
        do_login(client)
        rv = jpost(client, "/api/items", {"name": "Eggs", "section": "now", "list_id": lid})
        assert rv.status_code == 201
        item = rv.get_json()
        assert item["name"] == "Eggs"
        assert item["section"] == "now"
        assert item["is_bought"] == 0

    def test_add_item_records_history(self, client, app):
        make_user()
        lid = make_list()
        do_login(client)
        jpost(client, "/api/items", {"name": "Bananas", "section": "now", "list_id": lid})
        assert db_query(app, "SELECT name FROM item_name_history WHERE name = 'Bananas'")

    def test_add_item_optional_fields(self, client):
        make_user()
        lid = make_list()
        do_login(client)
        rv = jpost(client, "/api/items", {
            "name": "Cheese", "section": "later",
            "quantity": "200g", "notes": "aged", "list_id": lid,
        })
        assert rv.status_code == 201
        item = rv.get_json()
        assert item["quantity"] == "200g"
        assert item["notes"] == "aged"
        assert item["section"] == "later"

    def test_add_item_empty_name_rejected(self, client):
        make_user()
        lid = make_list()
        do_login(client)
        assert jpost(client, "/api/items", {"name": "", "section": "now", "list_id": lid}).status_code == 400

    def test_add_item_name_too_long_rejected(self, client):
        make_user()
        lid = make_list()
        do_login(client)
        assert jpost(client, "/api/items", {
            "name": "x" * 201, "section": "now", "list_id": lid
        }).status_code == 400

    def test_add_item_invalid_section_rejected(self, client):
        make_user()
        lid = make_list()
        do_login(client)
        assert jpost(client, "/api/items", {
            "name": "Milk", "section": "someday", "list_id": lid
        }).status_code == 400

    def test_add_item_quantity_too_long_rejected(self, client):
        make_user()
        lid = make_list()
        do_login(client)
        assert jpost(client, "/api/items", {
            "name": "Milk", "section": "now", "quantity": "x" * 51, "list_id": lid
        }).status_code == 400

    def test_add_item_notes_too_long_rejected(self, client):
        make_user()
        lid = make_list()
        do_login(client)
        assert jpost(client, "/api/items", {
            "name": "Milk", "section": "now", "notes": "x" * 501, "list_id": lid
        }).status_code == 400

    def test_toggle_marks_item_bought(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Cheese")
        do_login(client)
        rv = client.post(f"/api/items/{iid}/toggle")
        assert rv.status_code == 200
        row = db_query(app, "SELECT is_bought FROM items WHERE id = ?", (iid,))
        assert row["is_bought"] == 1

    def test_toggle_twice_returns_to_unbought(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Cheese")
        do_login(client)
        client.post(f"/api/items/{iid}/toggle")
        client.post(f"/api/items/{iid}/toggle")
        row = db_query(app, "SELECT is_bought FROM items WHERE id = ?", (iid,))
        assert row["is_bought"] == 0

    def test_move_now_to_later(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Butter", section="now")
        do_login(client)
        assert client.post(f"/api/items/{iid}/move").status_code == 200
        row = db_query(app, "SELECT section FROM items WHERE id = ?", (iid,))
        assert row["section"] == "later"

    def test_move_later_to_now(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Butter", section="later")
        do_login(client)
        client.post(f"/api/items/{iid}/move")
        row = db_query(app, "SELECT section FROM items WHERE id = ?", (iid,))
        assert row["section"] == "now"

    def test_delete_item(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Yogurt")
        do_login(client)
        rv = client.delete(f"/api/items/{iid}")
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True
        assert db_query(app, "SELECT id FROM items WHERE id = ?", (iid,)) is None

    def test_delete_item_writes_audit_log(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Yogurt")
        do_login(client)
        client.delete(f"/api/items/{iid}")
        row = db_query(app, "SELECT action FROM audit_log WHERE action = 'item.delete'")
        assert row is not None

    def test_clear_bought_removes_only_bought_items(self, client, app):
        make_user()
        lid = make_list()
        make_item(lid, "Done", is_bought=1)
        make_item(lid, "Pending", is_bought=0)
        do_login(client)
        rv = jpost(client, "/api/items/clear-bought", {"list_id": lid})
        assert rv.status_code == 200
        rows = []
        with app.app_context():
            rows = get_db().execute(
                "SELECT name FROM items WHERE list_id = ?", (lid,)
            ).fetchall()
        names = [r["name"] for r in rows]
        assert "Done" not in names
        assert "Pending" in names

    def test_clear_bought_scoped_to_list(self, client, app):
        make_user()
        lid1 = make_list("A")
        lid2 = make_list("B")
        make_item(lid1, "BoughtA", is_bought=1)
        make_item(lid2, "BoughtB", is_bought=1)
        do_login(client)
        jpost(client, "/api/items/clear-bought", {"list_id": lid1})
        assert db_query(app, "SELECT id FROM items WHERE name = 'BoughtA'") is None
        assert db_query(app, "SELECT id FROM items WHERE name = 'BoughtB'") is not None


# ---------------------------------------------------------------------------
# Item history
# ---------------------------------------------------------------------------


class TestItemHistory:
    def test_requires_auth(self, client):
        assert client.get("/api/items/history").status_code == 401

    def test_returns_empty_list(self, client):
        make_user()
        do_login(client)
        assert client.get("/api/items/history").get_json() == []

    def test_returns_history_names(self, client, app):
        make_user()
        do_login(client)
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO item_name_history (name) VALUES ('Apples')")
            db.execute("INSERT INTO item_name_history (name) VALUES ('Bread')")
            db.commit()
        names = client.get("/api/items/history").get_json()
        assert "Apples" in names and "Bread" in names

    def test_delete_history_entry(self, client, app):
        make_user()
        do_login(client)
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO item_name_history (name) VALUES ('Apples')")
            db.commit()
        rv = client.delete("/api/items/history/Apples")
        assert rv.status_code == 200
        assert db_query(app, "SELECT name FROM item_name_history WHERE name = 'Apples'") is None

    def test_delete_history_case_insensitive(self, client, app):
        make_user()
        do_login(client)
        with app.app_context():
            db = get_db()
            db.execute("INSERT INTO item_name_history (name) VALUES ('Apples')")
            db.commit()
        client.delete("/api/items/history/apples")
        assert db_query(app, "SELECT name FROM item_name_history WHERE name = 'Apples'") is None

    def test_add_item_upserts_history(self, client, app):
        make_user()
        lid = make_list()
        do_login(client)
        with app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO item_name_history (name, last_used) VALUES ('Milk', '2000-01-01')"
            )
            db.commit()
        jpost(client, "/api/items", {"name": "Milk", "section": "now", "list_id": lid})
        row = db_query(app, "SELECT last_used FROM item_name_history WHERE name = 'Milk'")
        assert row["last_used"] != "2000-01-01"


# ---------------------------------------------------------------------------
# Admin — user management
# ---------------------------------------------------------------------------


class TestAdminUsers:
    def _as_admin(self, client):
        make_user("admin_user", role="admin")
        do_login(client, "admin_user")

    def _admin_id(self, app):
        return db_query(app, "SELECT id FROM users WHERE username = 'admin_user'")["id"]

    def test_requires_auth(self, client):
        assert client.get("/admin/users").status_code == 302

    def test_non_admin_is_redirected(self, client):
        make_user()
        do_login(client)
        rv = client.get("/admin/users")
        assert rv.status_code == 302
        assert "/admin/users" not in rv.location

    def test_admin_sees_user_list(self, client):
        self._as_admin(client)
        rv = client.get("/admin/users")
        assert rv.status_code == 200
        assert b"admin_user" in rv.data

    def test_create_user_requires_must_change_password(self, client, app):
        self._as_admin(client)
        client.post("/admin/users/create", data={"username": "bob", "password": "pass1234"})
        row = db_query(app, "SELECT must_change_password FROM users WHERE username = 'bob'")
        assert row is not None
        assert row["must_change_password"] == 1

    def test_create_user_duplicate_username_rejected(self, client, app):
        self._as_admin(client)
        make_user("bob")
        client.post("/admin/users/create", data={"username": "bob", "password": "pass1234"})
        assert db_count(app, "SELECT COUNT(*) FROM users WHERE username = 'bob'") == 1

    def test_create_user_short_password_rejected(self, client, app):
        self._as_admin(client)
        client.post("/admin/users/create", data={"username": "carol", "password": "ab"})
        assert db_query(app, "SELECT id FROM users WHERE username = 'carol'") is None

    def test_disable_user_toggles_is_active(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        client.post(f"/admin/users/{uid}/disable")
        row = db_query(app, "SELECT is_active FROM users WHERE id = ?", (uid,))
        assert row["is_active"] == 0

    def test_reenable_user(self, client, app):
        self._as_admin(client)
        uid = make_user("bob", is_active=0)
        client.post(f"/admin/users/{uid}/disable")
        row = db_query(app, "SELECT is_active FROM users WHERE id = ?", (uid,))
        assert row["is_active"] == 1

    def test_cannot_disable_self(self, client, app):
        self._as_admin(client)
        admin_id = self._admin_id(app)
        client.post(f"/admin/users/{admin_id}/disable")
        row = db_query(app, "SELECT is_active FROM users WHERE id = ?", (admin_id,))
        assert row["is_active"] == 1

    def test_reset_password_sets_must_change(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        client.post(f"/admin/users/{uid}/reset-password", data={"password": "newpass99"})
        row = db_query(app, "SELECT must_change_password FROM users WHERE id = ?", (uid,))
        assert row["must_change_password"] == 1

    def test_reset_password_too_short_rejected(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        with app.app_context():
            old_hash = get_db().execute(
                "SELECT password_hash FROM users WHERE id = ?", (uid,)
            ).fetchone()["password_hash"]
        client.post(f"/admin/users/{uid}/reset-password", data={"password": "ab"})
        row = db_query(app, "SELECT password_hash FROM users WHERE id = ?", (uid,))
        assert row["password_hash"] == old_hash

    def test_delete_user(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        client.post(f"/admin/users/{uid}/delete")
        assert db_query(app, "SELECT id FROM users WHERE id = ?", (uid,)) is None

    def test_cannot_delete_self(self, client, app):
        self._as_admin(client)
        admin_id = self._admin_id(app)
        client.post(f"/admin/users/{admin_id}/delete")
        assert db_query(app, "SELECT id FROM users WHERE id = ?", (admin_id,)) is not None

    def test_delete_user_nullifies_item_attribution(self, client, app):
        """Items remain after their author is deleted; added_by becomes NULL."""
        self._as_admin(client)
        uid = make_user("bob")
        lid = make_list()
        iid = make_item(lid, "Soda", added_by=uid)
        client.post(f"/admin/users/{uid}/delete")
        row = db_query(app, "SELECT added_by FROM items WHERE id = ?", (iid,))
        assert row["added_by"] is None

    def test_disable_user_blocks_further_requests(self, client, app):
        """A user disabled mid-session cannot access protected routes."""
        self._as_admin(client)
        uid = make_user("bob")
        bob_client = app.test_client()
        do_login(bob_client, "bob")
        client.post(f"/admin/users/{uid}/disable")
        rv = bob_client.get("/")
        assert rv.status_code == 302
        assert "/login" in rv.location


# ---------------------------------------------------------------------------
# Admin — session revocation
# ---------------------------------------------------------------------------


class TestSessionRevocation:
    def _as_admin(self, client):
        make_user("admin_user", role="admin")
        do_login(client, "admin_user")

    def _admin_id(self, app):
        return db_query(app, "SELECT id FROM users WHERE username = 'admin_user'")["id"]

    def test_revoke_logs_out_existing_session(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        bob_client = app.test_client()
        do_login(bob_client, "bob")
        assert bob_client.get("/").status_code == 200
        client.post(f"/admin/users/{uid}/revoke-sessions")
        rv = bob_client.get("/")
        assert rv.status_code == 302
        assert "/login" in rv.location

    def test_revoke_logs_out_api_session(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        bob_client = app.test_client()
        do_login(bob_client, "bob")
        client.post(f"/admin/users/{uid}/revoke-sessions")
        assert bob_client.get("/api/lists").status_code == 401

    def test_user_can_log_in_again_after_revoke(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        bob_client = app.test_client()
        do_login(bob_client, "bob")
        client.post(f"/admin/users/{uid}/revoke-sessions")
        do_login(bob_client, "bob")
        assert bob_client.get("/").status_code == 200

    def test_self_revoke_keeps_current_session(self, client, app):
        """Revoking your own sessions logs out other devices but not this one."""
        self._as_admin(client)
        admin_id = self._admin_id(app)
        other_device = app.test_client()
        do_login(other_device, "admin_user")
        client.post(f"/admin/users/{admin_id}/revoke-sessions")
        assert client.get("/admin/users").status_code == 200
        rv = other_device.get("/")
        assert rv.status_code == 302
        assert "/login" in rv.location

    def test_revoke_writes_audit_log(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        client.post(f"/admin/users/{uid}/revoke-sessions")
        row = db_query(app, "SELECT detail FROM audit_log WHERE action = 'admin.sessions_revoke'")
        assert row is not None
        assert "bob" in row["detail"]

    def test_revoke_requires_admin(self, client, app):
        uid = make_user("bob")
        make_user("carol")
        do_login(client, "carol")
        client.post(f"/admin/users/{uid}/revoke-sessions")
        row = db_query(app, "SELECT session_epoch FROM users WHERE id = ?", (uid,))
        assert row["session_epoch"] == 0

    def test_stale_epoch_session_is_rejected(self, client, app):
        """A cookie minted before an epoch bump no longer authenticates."""
        make_user()
        do_login(client)
        with app.app_context():
            get_db().execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE username = 'alice'")
            get_db().commit()
        rv = client.get("/")
        assert rv.status_code == 302
        assert "/login" in rv.location


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    def test_x_frame_options_deny(self, client):
        make_user()
        do_login(client)
        rv = client.get("/")
        assert rv.headers.get("X-Frame-Options") == "DENY"

    def test_x_content_type_nosniff(self, client):
        make_user()
        do_login(client)
        assert client.get("/").headers.get("X-Content-Type-Options") == "nosniff"

    def test_referrer_policy(self, client):
        make_user()
        do_login(client)
        assert (
            client.get("/").headers.get("Referrer-Policy")
            == "strict-origin-when-cross-origin"
        )

    def test_csp_present_and_no_unsafe_inline_scripts(self, client):
        make_user()
        do_login(client)
        csp = client.get("/").headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp

    def test_headers_on_api_response(self, client):
        make_user()
        do_login(client)
        rv = client.get("/api/lists")
        assert rv.headers.get("X-Frame-Options") == "DENY"


# ---------------------------------------------------------------------------
# Auth guard — all protected endpoints redirect when unauthenticated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "/"),
        ("GET", "/change-password"),
        ("GET", "/admin/users"),
    ],
)
def test_unauthenticated_redirects_to_login(client, method, url):
    rv = client.open(url, method=method)
    assert rv.status_code == 302
    assert "/login" in rv.location


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "/api/lists"),
        ("GET", "/api/items"),
        ("GET", "/api/items/history"),
        ("GET", "/api/stream"),
        ("GET", "/api/csrf-token"),
    ],
)
def test_unauthenticated_api_returns_401_json(client, method, url):
    # API calls must get 401 (not a 302) so fetch() can detect expired sessions
    rv = client.open(url, method=method)
    assert rv.status_code == 401
    assert rv.get_json()["error"] == "unauthorized"


class TestCsrf:
    def test_csrf_token_endpoint_returns_token(self, client):
        make_user()
        do_login(client)
        rv = client.get("/api/csrf-token")
        assert rv.status_code == 200
        assert rv.get_json()["token"]

    def test_csrf_failure_on_api_returns_json(self, app, client):
        make_user()
        do_login(client)
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            rv = jpost(client, "/api/items", {"name": "Milk"})
        finally:
            app.config["WTF_CSRF_ENABLED"] = False
        assert rv.status_code == 400
        assert rv.get_json()["error"] == "csrf"

    def test_csrf_time_limit_disabled(self, app):
        # A timed token goes stale while the PWA sits open; tokens must remain
        # valid for the whole session instead.
        assert app.config["WTF_CSRF_TIME_LIMIT"] is None

    def test_csrf_failure_does_not_redirect_to_foreign_referrer(self, app, client):
        make_user()
        do_login(client)
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            rv = client.post("/logout", headers={"Referer": "https://evil.example/phish"})
        finally:
            app.config["WTF_CSRF_ENABLED"] = False
        assert rv.status_code == 302
        assert "evil.example" not in rv.location

    def test_csrf_failure_redirects_back_to_same_host_referrer(self, app, client):
        make_user()
        do_login(client)
        app.config["WTF_CSRF_ENABLED"] = True
        try:
            rv = client.post("/logout", headers={"Referer": "http://localhost/change-password"})
        finally:
            app.config["WTF_CSRF_ENABLED"] = False
        assert rv.status_code == 302
        assert rv.location.endswith("/change-password")


# ---------------------------------------------------------------------------
# PWA / service worker endpoints
# ---------------------------------------------------------------------------


class TestPWAEndpoints:
    def test_sw_js_served_with_no_cache(self, client):
        rv = client.get("/sw.js")
        assert rv.status_code == 200
        assert "no-cache" in rv.headers.get("Cache-Control", "")

    def test_manifest_served_with_correct_content_type(self, client):
        rv = client.get("/manifest.webmanifest")
        assert rv.status_code == 200
        assert "manifest" in rv.headers.get("Content-Type", "")
