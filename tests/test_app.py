"""
Regression tests for the GroceryList Flask app.

Covers: auth, lists API, items API, item history, change-password,
        admin user management, security headers, auth guards, and PWA endpoints.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

import config
from app import app as flask_app, get_db, pwa_asset_version, upgrade_db
from conftest import do_login, make_item, make_list, make_meal, make_recipe, make_user


# ---------------------------------------------------------------------------
# Tiny request helpers
# ---------------------------------------------------------------------------


def jpost(client, url, payload):
    return client.post(
        url, data=json.dumps(payload), content_type="application/json"
    )


def jput(client, url, payload):
    return client.put(
        url, data=json.dumps(payload), content_type="application/json"
    )


def db_query(app, sql, params=()):
    with app.app_context():
        return get_db().execute(sql, params).fetchone()


def db_count(app, sql, params=()):
    with app.app_context():
        return get_db().execute(sql, params).fetchone()[0]


def db_all(app, sql, params=()):
    with app.app_context():
        return get_db().execute(sql, params).fetchall()


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
        rv = client.post("/register", data={"setup_token": "test-bootstrap-token", "username": "alice", "password": "pass1234"})
        assert rv.status_code == 302
        row = db_query(app, "SELECT role FROM users WHERE username = 'alice'")
        assert row["role"] == "admin"

    def test_register_creates_session(self, client):
        client.post("/register", data={"setup_token": "test-bootstrap-token", "username": "alice", "password": "pass1234"})
        rv = client.get("/")
        assert rv.status_code == 200  # logged in, not redirected

    def test_empty_fields_rejected(self, client):
        rv = client.post("/register", data={"setup_token": "test-bootstrap-token", "username": "", "password": ""})
        assert rv.status_code == 400

    def test_short_password_rejected(self, client):
        rv = client.post("/register", data={"setup_token": "test-bootstrap-token", "username": "alice", "password": "ab"})
        assert rv.status_code == 400

    def test_claims_ownerless_lists_for_initial_admin(self, client, app):
        lid = make_list("Groceries")  # seeded before any user exists
        client.post("/register", data={"setup_token": "test-bootstrap-token", "username": "alice", "password": "pass1234"})
        uid = db_query(app, "SELECT id FROM users WHERE username = 'alice'")["id"]
        assert db_query(app, "SELECT created_by FROM lists WHERE id = ?", (lid,))[0] == uid

    def test_initial_admin_can_delete_claimed_seed_list(self, client):
        lid = make_list("Groceries")
        make_list("Second")
        client.post("/register", data={"setup_token": "test-bootstrap-token", "username": "alice", "password": "pass1234"})
        with client.session_transaction() as session:
            account = session["account_id"]
        assert client.delete(f"/api/lists/{lid}", headers={"X-Account-ID": account}).status_code == 200

    def test_invalid_bootstrap_token_rejected(self, client, app):
        rv = client.post("/register", data={
            "setup_token": "wrong", "username": "alice", "password": "pass1234",
        })
        assert rv.status_code == 403
        assert db_count(app, "SELECT COUNT(*) FROM users") == 0

    def test_non_ascii_bootstrap_token_rejected(self, client, app):
        rv = client.post("/register", data={
            "setup_token": "tökén", "username": "alice", "password": "pass1234",
        })
        assert rv.status_code == 403
        assert db_count(app, "SELECT COUNT(*) FROM users") == 0

    def test_setup_locked_without_bootstrap_token(self, client, monkeypatch):
        monkeypatch.setattr("config.BOOTSTRAP_TOKEN", "")
        rv = client.get("/register")
        assert rv.status_code == 503
        assert b"Initial setup is locked" in rv.data


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

    def test_long_password_matches_legacy_truncated_hash(self, client):
        # bcrypt<5 silently hashed only the first 72 bytes; those hashes must
        # keep verifying now that bcrypt>=5 rejects over-long input.
        long_password = "p" * 100
        make_user(password=long_password[:72])
        rv = do_login(client, password=long_password)
        assert rv.status_code == 302

    def test_long_password_for_unknown_user_returns_401(self, client):
        make_user()
        rv = do_login(client, username="nobody", password="p" * 100)
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

    def test_must_change_password_flag_redirects(self, client, app):
        make_user()
        do_login(client)
        with app.app_context():
            get_db().execute("UPDATE users SET must_change_password = 1")
            get_db().commit()
        rv = client.get("/")
        assert rv.status_code == 302
        assert "/change-password" in rv.location


class TestPlanningPages:
    @pytest.mark.parametrize("url", ["/meals", "/recipes", "/recipes/1"])
    def test_require_auth(self, client, url):
        rv = client.get(url)
        assert rv.status_code == 302
        assert "/login" in rv.location

    @pytest.mark.parametrize("url, active", [("/", "List"), ("/meals", "Meals"), ("/recipes", "Recipes")])
    def test_shared_nav_marks_current_page(self, client, url, active):
        make_user()
        do_login(client)
        html = client.get(url).get_data(as_text=True)
        assert 'class="app-nav"' in html
        assert f'active" aria-current="page">{active}</a>' in html

    def test_list_page_has_meal_strip_and_review_scripts(self, client):
        make_user()
        do_login(client)
        html = client.get("/").get_data(as_text=True)
        assert 'id="meal-strip"' in html
        assert "shopping-review.js" in html and "page-api.js" in html

    def test_recipe_detail_page_passes_id(self, client):
        make_user()
        do_login(client)
        html = client.get("/recipes/42").get_data(as_text=True)
        assert '<meta name="recipe-id" content="42">' in html


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

    def test_password_longer_than_72_bytes_can_be_set_and_used(self, client):
        make_user()
        do_login(client)
        long_password = "n" * 100
        rv = client.post(
            "/change-password",
            data={"current": "pass1234", "password": long_password, "confirm": long_password},
        )
        assert rv.status_code == 302
        client.post("/logout")
        rv = do_login(client, username="alice", password=long_password)
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
        uid = make_user()
        lid = make_list("First", created_by=uid)
        make_list("Second")
        do_login(client)
        rv = client.delete(f"/api/lists/{lid}")
        assert rv.status_code == 200
        assert rv.get_json()["ok"] is True

    def test_cannot_delete_last_list(self, client):
        uid = make_user()
        lid = make_list("Only", created_by=uid)
        do_login(client)
        assert client.delete(f"/api/lists/{lid}").status_code == 400

    def test_delete_list_requires_auth(self, client):
        lid = make_list()
        assert client.delete(f"/api/lists/{lid}").status_code == 401

    def test_admin_can_delete_any_list(self, client):
        make_user("admin", role="admin")
        other = make_user("bob")
        lid = make_list("Bob's list", created_by=other)
        make_list("Second")
        do_login(client, "admin")
        assert client.delete(f"/api/lists/{lid}").status_code == 200

    def test_non_creator_cannot_delete_list(self, client, app):
        make_user("alice")
        other = make_user("bob")
        lid = make_list("Bob's list", created_by=other)
        make_list("Second")
        do_login(client, "alice")
        rv = client.delete(f"/api/lists/{lid}")
        assert rv.status_code == 403
        assert db_query(app, "SELECT id FROM lists WHERE id = ?", (lid,))

    def test_non_creator_delete_leaves_items_intact(self, client, app):
        make_user("alice")
        other = make_user("bob")
        lid = make_list("Bob's list", created_by=other)
        make_list("Second")
        make_item(lid, "Milk")
        do_login(client, "alice")
        assert client.delete(f"/api/lists/{lid}").status_code == 403
        assert db_query(app, "SELECT id FROM items WHERE list_id = ?", (lid,))

    def test_creator_can_delete_own_list(self, client):
        uid = make_user("alice")
        lid = make_list("Alice's list", created_by=uid)
        make_list("Second")
        do_login(client, "alice")
        assert client.delete(f"/api/lists/{lid}").status_code == 200

    def test_legacy_ownerless_list_admin_only(self, client):
        make_user("alice")
        make_user("admin", role="admin")
        lid = make_list("Legacy")
        make_list("Second")
        do_login(client, "alice")
        assert client.delete(f"/api/lists/{lid}").status_code == 403

    def test_delete_missing_list_404s(self, client):
        make_user()
        make_list("Only")
        do_login(client)
        assert client.delete("/api/lists/9999").status_code == 404

    def test_get_reports_can_delete(self, client):
        uid = make_user("alice")
        other = make_user("bob")
        mine = make_list("Mine", created_by=uid)
        theirs = make_list("Theirs", created_by=other)
        do_login(client, "alice")
        flags = {d["id"]: d["can_delete"] for d in client.get("/api/lists").get_json()}
        assert flags[mine] is True
        assert flags[theirs] is False

    def test_get_reports_can_delete_for_admin(self, client):
        make_user("admin", role="admin")
        other = make_user("bob")
        theirs = make_list("Theirs", created_by=other)
        do_login(client, "admin")
        flags = {d["id"]: d["can_delete"] for d in client.get("/api/lists").get_json()}
        assert flags[theirs] is True


# ---------------------------------------------------------------------------
# Schema upgrade / backfill
# ---------------------------------------------------------------------------


class TestOwnerlessListBackfill:
    def test_upgrade_assigns_ownerless_lists_to_first_admin(self, app):
        make_user("bob")
        first_admin = make_user("admin", role="admin")
        make_user("admin2", role="admin")
        lid = make_list("Legacy")
        upgrade_db()
        assert db_query(app, "SELECT created_by FROM lists WHERE id = ?", (lid,))[0] == first_admin

    def test_upgrade_leaves_owned_lists_alone(self, app):
        owner = make_user("bob")
        make_user("admin", role="admin")
        lid = make_list("Bob's list", created_by=owner)
        upgrade_db()
        assert db_query(app, "SELECT created_by FROM lists WHERE id = ?", (lid,))[0] == owner

    def test_upgrade_without_admin_leaves_list_ownerless(self, app):
        make_user("bob")
        lid = make_list("Legacy")
        upgrade_db()
        assert db_query(app, "SELECT created_by FROM lists WHERE id = ?", (lid,))[0] is None


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

    def test_update_item(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Milk", section="now")
        do_login(client)
        rv = jput(client, f"/api/items/{iid}", {
            "name": "Whole Milk", "section": "later",
            "quantity": "2L", "notes": "cold", "list_id": lid,
        })
        assert rv.status_code == 200
        item = rv.get_json()
        assert item["name"] == "Whole Milk"
        assert item["section"] == "later"
        assert item["quantity"] == "2L"
        assert item["notes"] == "cold"
        row = db_query(app, "SELECT name, section, quantity, notes FROM items WHERE id = ?", (iid,))
        assert row["name"] == "Whole Milk"
        assert row["section"] == "later"
        assert row["quantity"] == "2L"
        assert row["notes"] == "cold"

    def test_update_item_clears_optional_fields(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Milk")
        do_login(client)
        jput(client, f"/api/items/{iid}", {"quantity": "1L", "notes": "cold"})
        jput(client, f"/api/items/{iid}", {"name": "Milk", "quantity": "", "notes": ""})
        row = db_query(app, "SELECT quantity, notes FROM items WHERE id = ?", (iid,))
        assert row["quantity"] is None
        assert row["notes"] is None

    def test_update_item_preserves_bought_status(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Milk", is_bought=1)
        do_login(client)
        jput(client, f"/api/items/{iid}", {"name": "Skim Milk"})
        row = db_query(app, "SELECT is_bought FROM items WHERE id = ?", (iid,))
        assert row["is_bought"] == 1

    def test_update_item_records_history(self, client, app):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Milk")
        do_login(client)
        jput(client, f"/api/items/{iid}", {"name": "Oat Milk"})
        assert db_query(app, "SELECT name FROM item_name_history WHERE name = 'Oat Milk'")

    def test_update_item_empty_name_rejected(self, client):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Milk")
        do_login(client)
        assert jput(client, f"/api/items/{iid}", {"name": ""}).status_code == 400

    def test_update_item_name_too_long_rejected(self, client):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Milk")
        do_login(client)
        assert jput(client, f"/api/items/{iid}", {"name": "x" * 201}).status_code == 400

    def test_update_item_invalid_section_rejected(self, client):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Milk")
        do_login(client)
        assert jput(client, f"/api/items/{iid}", {
            "name": "Milk", "section": "someday"
        }).status_code == 400

    def test_update_item_quantity_too_long_rejected(self, client):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Milk")
        do_login(client)
        assert jput(client, f"/api/items/{iid}", {
            "name": "Milk", "quantity": "x" * 51
        }).status_code == 400

    def test_update_item_notes_too_long_rejected(self, client):
        make_user()
        lid = make_list()
        iid = make_item(lid, "Milk")
        do_login(client)
        assert jput(client, f"/api/items/{iid}", {
            "name": "Milk", "notes": "x" * 501
        }).status_code == 400

    def test_update_nonexistent_item_returns_404(self, client):
        make_user()
        do_login(client)
        assert jput(client, "/api/items/9999", {"name": "Ghost"}).status_code == 404

    def test_update_item_requires_auth(self, client):
        lid = make_list()
        iid = make_item(lid, "Milk")
        assert jput(client, f"/api/items/{iid}", {"name": "Milk"}).status_code == 401

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

    def test_delete_user_nullifies_recipe_attribution(self, client, app):
        """Recipes remain usable after their creator is deleted."""
        self._as_admin(client)
        uid = make_user("bob")
        recipe_id = make_recipe(created_by=uid)

        rv = client.post(f"/admin/users/{uid}/delete")

        assert rv.status_code == 302
        assert db_query(app, "SELECT id FROM users WHERE id = ?", (uid,)) is None
        recipe = db_query(app, "SELECT created_by FROM recipes WHERE id = ?", (recipe_id,))
        assert recipe is not None
        assert recipe["created_by"] is None

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
# Password changes revoke sessions
# ---------------------------------------------------------------------------


class TestPasswordChangeRevokesSessions:
    """Setting a password -- by the user or by an admin -- must evict every
    cookie issued before it. session_epoch is the only server-side lever for
    this, so both write paths have to bump it."""

    NEW = {"current": "pass1234", "password": "newpass99", "confirm": "newpass99"}

    def _as_admin(self, client):
        make_user("admin_user", role="admin")
        do_login(client, "admin_user")

    # --- self-service change ---

    def test_change_password_logs_out_other_sessions(self, client, app):
        make_user()
        do_login(client)
        other_device = app.test_client()
        do_login(other_device, "alice")
        assert other_device.get("/").status_code == 200

        client.post("/change-password", data=self.NEW)

        rv = other_device.get("/")
        assert rv.status_code == 302
        assert "/login" in rv.location

    def test_change_password_logs_out_other_api_sessions(self, client, app):
        make_user()
        do_login(client)
        other_device = app.test_client()
        do_login(other_device, "alice")
        client.post("/change-password", data=self.NEW)
        assert other_device.get("/api/lists").status_code == 401

    def test_change_password_keeps_current_session(self, client, app):
        """The device that made the change stays logged in."""
        make_user()
        do_login(client)
        client.post("/change-password", data=self.NEW)
        assert client.get("/").status_code == 200

    def test_change_password_bumps_epoch(self, client, app):
        make_user()
        do_login(client)
        client.post("/change-password", data=self.NEW)
        row = db_query(app, "SELECT session_epoch FROM users WHERE username = 'alice'")
        assert row["session_epoch"] == 1

    def test_login_with_new_password_after_change(self, client, app):
        """A fresh login picks up the bumped epoch and authenticates."""
        make_user()
        do_login(client)
        client.post("/change-password", data=self.NEW)
        fresh = app.test_client()
        do_login(fresh, "alice", "newpass99")
        assert fresh.get("/").status_code == 200

    def test_failed_change_does_not_bump_epoch(self, client, app):
        """A rejected change must not log the user out of anything."""
        make_user()
        do_login(client)
        other_device = app.test_client()
        do_login(other_device, "alice")
        client.post(
            "/change-password",
            data={"current": "wrongpass", "password": "newpass99", "confirm": "newpass99"},
        )
        row = db_query(app, "SELECT session_epoch FROM users WHERE username = 'alice'")
        assert row["session_epoch"] == 0
        assert other_device.get("/").status_code == 200

    # --- admin reset ---

    def test_admin_reset_logs_out_target_sessions(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        bob_client = app.test_client()
        do_login(bob_client, "bob")
        assert bob_client.get("/").status_code == 200

        client.post(f"/admin/users/{uid}/reset-password", data={"password": "temp12345"})

        rv = bob_client.get("/")
        assert rv.status_code == 302
        assert "/login" in rv.location

    def test_admin_reset_bumps_target_epoch(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        client.post(f"/admin/users/{uid}/reset-password", data={"password": "temp12345"})
        row = db_query(app, "SELECT session_epoch FROM users WHERE id = ?", (uid,))
        assert row["session_epoch"] == 1

    def test_stale_session_cannot_hijack_account_after_admin_reset(self, client, app):
        """Regression: change_password() reads must_change_password from the DB,
        so before the epoch bump an attacker holding a pre-reset cookie could
        POST a password of their own choosing with no current-password check --
        turning the admin's remediation into a full account takeover."""
        self._as_admin(client)
        uid = make_user("bob")
        attacker = app.test_client()
        do_login(attacker, "bob")

        client.post(f"/admin/users/{uid}/reset-password", data={"password": "temp12345"})

        rv = attacker.post(
            "/change-password", data={"password": "pwned1234", "confirm": "pwned1234"}
        )
        assert rv.status_code == 302
        assert "/login" in rv.location

        # The attacker's password was never set; the admin's temp one still stands.
        victim = app.test_client()
        assert victim.post(
            "/login", data={"username": "bob", "password": "pwned1234"}
        ).status_code == 401
        rv = do_login(victim, "bob", "temp12345")
        assert rv.status_code == 302
        assert "/change-password" in rv.location

    def test_admin_self_reset_keeps_current_session(self, client, app):
        """Resetting your own password shouldn't log you out mid-request."""
        self._as_admin(client)
        admin_id = db_query(app, "SELECT id FROM users WHERE username = 'admin_user'")["id"]
        client.post(f"/admin/users/{admin_id}/reset-password", data={"password": "temp12345"})
        assert client.get("/admin/users").location.endswith("/change-password")
        assert client.get("/change-password").status_code == 200

    def test_admin_reset_does_not_touch_other_users_sessions(self, client, app):
        self._as_admin(client)
        uid = make_user("bob")
        make_user("carol")
        carol_client = app.test_client()
        do_login(carol_client, "carol")
        client.post(f"/admin/users/{uid}/reset-password", data={"password": "temp12345"})
        assert carol_client.get("/").status_code == 200

    def test_failed_admin_reset_does_not_bump_epoch(self, client, app):
        """A rejected reset (too-short password) must not revoke sessions."""
        self._as_admin(client)
        uid = make_user("bob")
        bob_client = app.test_client()
        do_login(bob_client, "bob")
        client.post(f"/admin/users/{uid}/reset-password", data={"password": "ab"})
        row = db_query(app, "SELECT session_epoch FROM users WHERE id = ?", (uid,))
        assert row["session_epoch"] == 0
        assert bob_client.get("/").status_code == 200


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    def test_http_redirects_to_https_when_enforced(self, client, monkeypatch):
        make_user()
        monkeypatch.setattr("config.ENFORCE_HTTPS", True)
        rv = client.get("/login")
        assert rv.status_code == 308
        assert rv.location == "https://localhost/login"

    def test_trusted_https_proxy_sets_hsts(self, client, monkeypatch):
        make_user()
        monkeypatch.setattr("config.ENFORCE_HTTPS", True)
        rv = client.get("/login", headers={"X-Forwarded-Proto": "https"})
        assert rv.status_code == 200
        assert rv.headers["Strict-Transport-Security"] == "max-age=31536000"

    def test_trusted_proxy_supplies_rate_limit_client_address(self, client):
        seen = []

        def capture_remote_address():
            from flask import request
            seen.append(request.remote_addr)

        flask_app.before_request_funcs.setdefault(None, []).append(capture_remote_address)
        try:
            client.get("/register", headers={"X-Forwarded-For": "203.0.113.42"})
        finally:
            flask_app.before_request_funcs[None].remove(capture_remote_address)

        assert seen == ["203.0.113.42"]

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
# Removed external API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method, url", [("GET", "/api/external/lists"), ("POST", "/api/external/items")])
def test_external_api_is_gone(client, method, url):
    # The token-authenticated companion API was removed with MenuPlanner;
    # a bearer token must not unlock anything.
    make_list()
    rv = client.open(url, method=method, json={"items": [{"name": "Milk"}]},
                     headers={"Authorization": "Bearer anything"})
    assert rv.status_code == 404


# ---------------------------------------------------------------------------
# PWA / service worker endpoints
# ---------------------------------------------------------------------------


class TestPWAEndpoints:
    def test_sw_js_served_with_no_cache(self, client):
        rv = client.get("/sw.js")
        assert rv.status_code == 200
        assert "no-cache" in rv.headers.get("Cache-Control", "")
        assert "__ASSET_VERSION__" not in rv.get_data(as_text=True)
        assert f"grocery-static-{pwa_asset_version()}" in rv.get_data(as_text=True)
        assert rv.headers["Service-Worker-Allowed"] == "/"

    def test_manifest_served_with_correct_content_type(self, client):
        rv = client.get("/manifest.webmanifest")
        assert rv.status_code == 200
        assert "manifest" in rv.headers.get("Content-Type", "")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthz:
    def test_healthy_before_initial_setup(self, client, monkeypatch):
        # Fresh install: no users and setup locked must not fail the health check.
        monkeypatch.setattr("config.BOOTSTRAP_TOKEN", "")
        rv = client.get("/healthz")
        assert rv.status_code == 200
        assert rv.data == b"ok\n"

    def test_not_redirected_when_https_enforced(self, client, monkeypatch):
        monkeypatch.setattr("config.ENFORCE_HTTPS", True)
        assert client.get("/healthz").status_code == 200

    def test_other_routes_still_redirected(self, client, monkeypatch):
        monkeypatch.setattr("config.ENFORCE_HTTPS", True)
        assert client.get("/healthz/extra").status_code == 308

    def test_database_failure_reports_unavailable(self, client, monkeypatch, tmp_path):
        monkeypatch.setattr("config.DATABASE", str(tmp_path / "missing" / "grocery.db"))
        rv = client.get("/healthz")
        assert rv.status_code == 503
        assert b"missing" not in rv.data


# ---------------------------------------------------------------------------
# Recipes API
# ---------------------------------------------------------------------------


class TestRecipesAPI:
    def test_requires_auth(self, client):
        assert client.get("/api/recipes").status_code == 401

    def test_get_returns_empty_list(self, client):
        make_user()
        do_login(client)
        assert client.get("/api/recipes").get_json() == []

    def test_create_recipe(self, client, app):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/recipes", {
            "name": "Pancakes",
            "notes": "Kids loved these",
            "ingredients": ["2 cups flour", "1 egg", "  "],
            "steps": ["Mix", "Cook"],
        })
        assert rv.status_code == 201
        recipe_id = rv.get_json()["id"]
        row = db_query(app, "SELECT name, notes, steps FROM recipes WHERE id = ?", (recipe_id,))
        assert row["name"] == "Pancakes"
        assert row["notes"] == "Kids loved these"
        assert row["steps"] == "Mix\nCook"
        rows = db_all(
            app, "SELECT name, qty, unit FROM recipe_ingredients WHERE recipe_id = ? ORDER BY sort", (recipe_id,)
        )
        # Free-text lines are parsed; the blank line is dropped.
        assert [(r["name"], r["qty"], r["unit"]) for r in rows] == [("flour", 2.0, "cup"), ("egg", 1.0, "")]

    def test_create_recipe_requires_name(self, client):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/recipes", {"name": "", "ingredients": ["Flour"]})
        assert rv.status_code == 400

    def test_create_recipe_requires_ingredient(self, client):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/recipes", {"name": "Empty", "ingredients": []})
        assert rv.status_code == 400

    def test_create_recipe_name_too_long_rejected(self, client):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/recipes", {"name": "x" * 201, "ingredients": ["Flour"]})
        assert rv.status_code == 400

    def test_create_recipe_too_many_ingredients_rejected(self, client):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/recipes", {"name": "Big", "ingredients": ["x"] * 201})
        assert rv.status_code == 400

    def test_get_recipe_detail(self, client):
        uid = make_user()
        rid = make_recipe(name="Soup", notes="Good", steps=["Chop", "Simmer"],
                           ingredients=["Carrots", "Broth"], created_by=uid)
        do_login(client)
        rv = client.get(f"/api/recipes/{rid}")
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["name"] == "Soup"
        assert data["steps"] == "Chop\nSimmer"
        assert [i["name"] for i in data["ingredients"]] == ["Carrots", "Broth"]
        assert data["can_delete"] is True

    def test_get_recipe_not_found(self, client):
        make_user()
        do_login(client)
        assert client.get("/api/recipes/999").status_code == 404

    def test_update_recipe_replaces_ingredients(self, client, app):
        uid = make_user()
        rid = make_recipe(ingredients=["Old1", "Old2"], created_by=uid)
        do_login(client)
        rv = jput(client, f"/api/recipes/{rid}", {
            "name": "Updated", "notes": None, "steps": ["Step1"], "ingredients": ["New1"],
        })
        assert rv.status_code == 200
        names = [r["name"] for r in db_all(
            app, "SELECT name FROM recipe_ingredients WHERE recipe_id = ? ORDER BY sort", (rid,)
        )]
        assert names == ["New1"]

    def test_update_recipe_not_found(self, client):
        make_user()
        do_login(client)
        rv = jput(client, "/api/recipes/999", {"name": "X", "ingredients": ["Y"]})
        assert rv.status_code == 404

    def test_delete_recipe_by_creator(self, client, app):
        uid = make_user()
        rid = make_recipe(created_by=uid)
        do_login(client)
        rv = client.delete(f"/api/recipes/{rid}")
        assert rv.status_code == 200
        assert db_query(app, "SELECT id FROM recipes WHERE id = ?", (rid,)) is None

    def test_delete_recipe_cascades_ingredients(self, client, app):
        uid = make_user()
        rid = make_recipe(created_by=uid, ingredients=["A", "B"])
        do_login(client)
        client.delete(f"/api/recipes/{rid}")
        assert db_count(app, "SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id = ?", (rid,)) == 0

    def test_delete_recipe_requires_auth(self, client):
        rid = make_recipe()
        assert client.delete(f"/api/recipes/{rid}").status_code == 401

    def test_non_creator_cannot_delete_recipe(self, client, app):
        make_user("alice")
        other = make_user("bob")
        rid = make_recipe(created_by=other)
        do_login(client, "alice")
        rv = client.delete(f"/api/recipes/{rid}")
        assert rv.status_code == 403
        assert db_query(app, "SELECT id FROM recipes WHERE id = ?", (rid,))

    def test_admin_can_delete_any_recipe(self, client):
        make_user("admin", role="admin")
        other = make_user("bob")
        rid = make_recipe(created_by=other)
        do_login(client, "admin")
        assert client.delete(f"/api/recipes/{rid}").status_code == 200

    def test_structured_ingredients_and_servings_saved(self, client, app):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/recipes", {
            "name": "Tacos", "servings": 4, "source_url": "https://example.com/tacos",
            "ingredients": [
                {"name": "Ground beef", "qty": "1 1/2", "unit": "lb", "note": ""},
                {"name": "", "qty": "2"},  # half-filled editor row is dropped
                {"name": "Salt"},
            ],
        })
        assert rv.status_code == 201
        rows = db_all(app, "SELECT name, qty, unit FROM recipe_ingredients ORDER BY sort")
        assert [(r["name"], r["qty"], r["unit"]) for r in rows] == [("Ground beef", 1.5, "lb"), ("Salt", None, "")]
        recipe = db_query(app, "SELECT servings, source_url FROM recipes")
        assert (recipe["servings"], recipe["source_url"]) == (4, "https://example.com/tacos")

    @pytest.mark.parametrize("payload, message", [
        ({"servings": 0}, "Servings"),
        ({"servings": "4"}, "Servings"),
        ({"source_url": "javascript:alert(1)"}, "Source link"),
        ({"ingredients": [{"name": "Beef", "qty": "lots"}]}, "amount"),
        ({"ingredients": [{"name": "Beef", "qty": -1}]}, "amount"),
        ({"ingredients": [{"name": "Beef", "unit": 5}]}, "unit must be a string"),
    ])
    def test_invalid_recipe_fields_rejected(self, client, payload, message):
        make_user()
        do_login(client)
        body = {"name": "Tacos", "ingredients": ["1 lb beef"], **payload}
        rv = jpost(client, "/api/recipes", body)
        assert rv.status_code == 400
        assert message in rv.get_json()["error"]

    def test_detail_scales_to_requested_servings(self, client):
        uid = make_user()
        rid = make_recipe(ingredients=[("Milk", 1, "cup"), ("Salt", None, "")], servings=4, created_by=uid)
        do_login(client)
        data = client.get(f"/api/recipes/{rid}?servings=6").get_json()
        assert data["scaled_servings"] == 6
        assert [(i["name"], i["amount"]) for i in data["ingredients"]] == [("Milk", "1½ cups"), ("Salt", "")]

    def test_search_filters_by_name(self, client):
        uid = make_user()
        make_recipe(name="Chicken fajitas", created_by=uid)
        make_recipe(name="Pancakes", created_by=uid)
        make_recipe(name="100% rye", created_by=uid)
        do_login(client)
        assert [r["name"] for r in client.get("/api/recipes?q=CHICK").get_json()] == ["Chicken fajitas"]
        assert [r["name"] for r in client.get("/api/recipes?q=%25").get_json()] == ["100% rye"]

    def test_parse_ingredients_splits_lines(self, client):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/recipes/parse-ingredients", {"lines": ["1 1/2 cups flour, sifted", ""]})
        assert rv.status_code == 200
        assert rv.get_json()["ingredients"] == [
            {"name": "flour", "qty": 1.5, "unit": "cup", "note": "sifted", "qty_text": "1½"},
        ]

    def test_deleting_recipe_keeps_planned_meal(self, client, app):
        uid = make_user()
        rid = make_recipe(name="Tacos", created_by=uid)
        meal_id = make_meal("2026-10-13", title="Tacos", recipe_id=rid)
        do_login(client)
        assert client.delete(f"/api/recipes/{rid}").status_code == 200
        row = db_query(app, "SELECT title, recipe_id FROM planned_meals WHERE id = ?", (meal_id,))
        assert (row["title"], row["recipe_id"]) == ("Tacos", None)


# ---------------------------------------------------------------------------
# Meal planning API
# ---------------------------------------------------------------------------


class TestMealsAPI:
    def test_requires_auth(self, client):
        assert client.get("/api/meals?start=2026-10-12").status_code == 401
        assert jpost(client, "/api/meals", {"date": "2026-10-12", "title": "X"}).status_code == 401

    def test_writes_require_matching_account_header(self, client):
        make_user()
        do_login(client)
        client.environ_base["HTTP_X_ACCOUNT_ID"] = "someone-else"
        assert jpost(client, "/api/meals", {"date": "2026-10-12", "title": "X"}).status_code == 409

    def test_create_with_recipe_defaults_title(self, client):
        uid = make_user()
        rid = make_recipe(name="Tacos", created_by=uid)
        do_login(client)
        rv = jpost(client, "/api/meals", {"date": "2026-10-13", "kind": "cook", "recipe_id": rid, "servings": 6})
        assert rv.status_code == 201
        meal = rv.get_json()
        assert (meal["title"], meal["recipe_name"], meal["servings"], meal["kind"]) == ("Tacos", "Tacos", 6, "cook")

    def test_list_returns_requested_range_in_order(self, client):
        make_user()
        make_meal("2026-10-19", title="Next week")
        make_meal("2026-10-14", title="Wed")
        make_meal("2026-10-12", title="Mon")
        make_meal("2026-10-12", title="Mon too", kind="flexible")
        do_login(client)
        rows = client.get("/api/meals?start=2026-10-12&days=7").get_json()
        assert [m["title"] for m in rows] == ["Mon", "Mon too", "Wed"]

    @pytest.mark.parametrize("query", ["start=2026-13-01", "start=yesterday", "start=2026-10-12&days=0",
                                       "start=2026-10-12&days=100", ""])
    def test_list_rejects_bad_range(self, client, query):
        make_user()
        do_login(client)
        assert client.get(f"/api/meals?{query}").status_code == 400

    def test_leftovers_title_from_cook_night(self, client):
        make_user()
        cook = make_meal("2026-10-13", title="Chili")
        do_login(client)
        rv = jpost(client, "/api/meals", {"date": "2026-10-14", "kind": "leftovers", "leftover_of": cook})
        assert rv.status_code == 201
        assert rv.get_json()["title"] == "Leftover Chili"
        assert rv.get_json()["leftover_of_date"] == "2026-10-13"

    def test_leftovers_validation(self, client):
        make_user()
        cook = make_meal("2026-10-13", title="Chili")
        flex = make_meal("2026-10-12", title="Whatever", kind="flexible")
        do_login(client)
        before = jpost(client, "/api/meals", {"date": "2026-10-12", "kind": "leftovers", "leftover_of": cook})
        assert before.status_code == 400
        not_cook = jpost(client, "/api/meals", {"date": "2026-10-14", "kind": "leftovers", "leftover_of": flex})
        assert not_cook.status_code == 400

    @pytest.mark.parametrize("payload", [
        {"date": "2026-10-12"},  # no recipe or title
        {"date": "2026-02-30", "title": "X"},
        {"date": "2026-10-12", "title": "X", "kind": "brunch"},
        {"date": "2026-10-12", "title": "X", "servings": 0},
        {"date": "2026-10-12", "recipe_id": 999999},
        {"date": "2026-10-12", "title": "x" * 201},
    ])
    def test_create_rejects_invalid(self, client, payload):
        make_user()
        do_login(client)
        assert jpost(client, "/api/meals", payload).status_code == 400

    def test_update_moves_meal_and_keeps_leftover_link(self, client, app):
        make_user()
        cook = make_meal("2026-10-13", title="Chili")
        left = make_meal("2026-10-15", title="Leftover Chili", kind="leftovers", leftover_of=cook)
        do_login(client)
        rv = jput(client, f"/api/meals/{cook}", {"date": "2026-10-14", "kind": "cook", "title": "Chili"})
        assert rv.status_code == 200
        assert rv.get_json()["date"] == "2026-10-14"
        assert db_query(app, "SELECT leftover_of FROM planned_meals WHERE id = ?", (left,))["leftover_of"] == cook

    def test_changing_cook_night_kind_unlinks_leftovers(self, client, app):
        make_user()
        cook = make_meal("2026-10-13", title="Chili")
        left = make_meal("2026-10-15", title="Leftover Chili", kind="leftovers", leftover_of=cook)
        do_login(client)
        jput(client, f"/api/meals/{cook}", {"date": "2026-10-13", "kind": "flexible", "title": "Chili"})
        assert db_query(app, "SELECT leftover_of FROM planned_meals WHERE id = ?", (left,))["leftover_of"] is None

    def test_delete(self, client, app):
        make_user()
        meal = make_meal("2026-10-13")
        do_login(client)
        assert client.delete(f"/api/meals/{meal}").status_code == 200
        assert client.delete(f"/api/meals/{meal}").status_code == 404
        assert db_count(app, "SELECT COUNT(*) FROM planned_meals") == 0


class TestWeekMenu:
    def test_unscheduled_meal_lands_on_week_monday(self, client):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/meals", {"date": "2026-10-15", "title": "Tacos", "unscheduled": True})
        assert rv.status_code == 201
        meal = rv.get_json()
        assert (meal["date"], meal["unscheduled"]) == ("2026-10-12", 1)
        rows = client.get("/api/meals?start=2026-10-12&days=7").get_json()
        assert [(m["title"], m["unscheduled"]) for m in rows] == [("Tacos", 1)]

    def test_picking_a_day_schedules_it(self, client):
        make_user()
        do_login(client)
        meal = jpost(client, "/api/meals", {"date": "2026-10-12", "title": "Tacos", "unscheduled": True}).get_json()
        rv = jput(client, f"/api/meals/{meal['id']}", {"date": "2026-10-16", "title": "Tacos", "unscheduled": False})
        assert rv.status_code == 200
        assert (rv.get_json()["date"], rv.get_json()["unscheduled"]) == ("2026-10-16", 0)

    def test_leftovers_cannot_be_unscheduled(self, client):
        make_user()
        do_login(client)
        rv = jpost(client, "/api/meals", {"date": "2026-10-12", "title": "X", "kind": "leftovers", "unscheduled": True})
        assert rv.status_code == 400

    def test_leftovers_need_a_scheduled_cook_night(self, client):
        make_user()
        do_login(client)
        cook = jpost(client, "/api/meals", {"date": "2026-10-12", "title": "Chili", "unscheduled": True}).get_json()
        rv = jpost(client, "/api/meals", {"date": "2026-10-14", "kind": "leftovers", "leftover_of": cook["id"]})
        assert rv.status_code == 400

    def test_cook_night_with_leftovers_stays_on_a_day(self, client):
        make_user()
        cook = make_meal("2026-10-13", title="Chili")
        make_meal("2026-10-15", title="Leftover Chili", kind="leftovers", leftover_of=cook)
        do_login(client)
        rv = jput(client, f"/api/meals/{cook}", {"date": "2026-10-13", "title": "Chili", "unscheduled": True})
        assert rv.status_code == 400

    def test_rejects_non_boolean(self, client):
        make_user()
        do_login(client)
        assert jpost(client, "/api/meals", {"date": "2026-10-12", "title": "X", "unscheduled": "yes"}).status_code == 400

    def test_upgrade_adds_column_to_old_table(self, app):
        with app.app_context():
            db = get_db()
            db.execute("DROP TABLE planned_meals")
            db.execute("CREATE TABLE planned_meals (id INTEGER PRIMARY KEY, date TEXT NOT NULL, "
                       "kind TEXT NOT NULL DEFAULT 'cook', recipe_id INTEGER, title TEXT NOT NULL, "
                       "leftover_of INTEGER, servings INTEGER, notes TEXT, sort INTEGER NOT NULL DEFAULT 0, "
                       "added_to_list_id INTEGER, added_to_list_at TEXT, created_by INTEGER, "
                       "created_at TEXT, updated_at TEXT)")
            db.execute("INSERT INTO planned_meals (date, title) VALUES ('2026-10-12', 'Old')")
            db.commit()
        upgrade_db()
        assert db_query(app, "SELECT unscheduled FROM planned_meals")["unscheduled"] == 0


class TestKidsAndLunches:
    def test_requires_auth(self, client):
        assert client.get("/api/kids").status_code == 401
        assert client.get("/api/lunches?start=2026-10-12").status_code == 401

    def test_kid_crud(self, client, app):
        make_user()
        do_login(client)
        a = jpost(client, "/api/kids", {"name": " Ava "}).get_json()
        jpost(client, "/api/kids", {"name": "Ben"})
        assert a["name"] == "Ava"
        assert [k["name"] for k in client.get("/api/kids").get_json()] == ["Ava", "Ben"]
        assert jput(client, f"/api/kids/{a['id']}", {"name": "Avery"}).status_code == 200
        assert jpost(client, "/api/kids", {"name": "  "}).status_code == 400
        assert jput(client, "/api/kids/99999", {"name": "X"}).status_code == 404
        assert client.delete(f"/api/kids/{a['id']}").status_code == 200
        assert [k["name"] for k in client.get("/api/kids").get_json()] == ["Ben"]

    def test_set_update_and_clear_lunch(self, client, app):
        make_user()
        do_login(client)
        kid = jpost(client, "/api/kids", {"name": "Ava"}).get_json()["id"]
        rv = jput(client, "/api/lunches", {"kid_id": kid, "date": "2026-10-13", "kind": "school", "choice": "Pizza"})
        assert rv.status_code == 200
        jput(client, "/api/lunches", {"kid_id": kid, "date": "2026-10-13", "kind": "home", "choice": "PB&J"})
        rows = client.get("/api/lunches?start=2026-10-12&days=7").get_json()
        assert rows == [{"kid_id": kid, "date": "2026-10-13", "kind": "home", "choice": "PB&J"}]
        assert jput(client, "/api/lunches", {"kid_id": kid, "date": "2026-10-13", "kind": None}).status_code == 200
        assert db_count(app, "SELECT COUNT(*) FROM kid_lunches") == 0

    @pytest.mark.parametrize("payload", [
        {"date": "2026-02-30", "kind": "home"},
        {"date": "2026-10-13", "kind": "cafeteria"},
        {"date": "2026-10-13", "kind": "home", "choice": "x" * 201},
        {"date": "2026-10-13", "kind": "home", "choice": 5},
    ])
    def test_set_lunch_rejects_invalid(self, client, payload):
        make_user()
        do_login(client)
        kid = jpost(client, "/api/kids", {"name": "Ava"}).get_json()["id"]
        assert jput(client, "/api/lunches", dict(payload, kid_id=kid)).status_code == 400

    def test_set_lunch_unknown_kid(self, client):
        make_user()
        do_login(client)
        assert jput(client, "/api/lunches", {"kid_id": 99999, "date": "2026-10-13", "kind": "home"}).status_code == 404

    def test_deleting_kid_removes_lunches(self, client, app):
        make_user()
        do_login(client)
        kid = jpost(client, "/api/kids", {"name": "Ava"}).get_json()["id"]
        jput(client, "/api/lunches", {"kid_id": kid, "date": "2026-10-13", "kind": "home"})
        client.delete(f"/api/kids/{kid}")
        assert db_count(app, "SELECT COUNT(*) FROM kid_lunches") == 0

    def test_copy_week_fills_only_unset_days(self, client):
        make_user()
        do_login(client)
        kid = jpost(client, "/api/kids", {"name": "Ava"}).get_json()["id"]
        jput(client, "/api/lunches", {"kid_id": kid, "date": "2026-10-12", "kind": "school", "choice": "Pizza"})
        jput(client, "/api/lunches", {"kid_id": kid, "date": "2026-10-13", "kind": "home"})
        jput(client, "/api/lunches", {"kid_id": kid, "date": "2026-10-20", "kind": "school", "choice": "Tacos"})
        rv = jpost(client, "/api/lunches/copy-week", {"from": "2026-10-12", "to": "2026-10-19"})
        assert rv.get_json() == {"copied": 1}
        rows = client.get("/api/lunches?start=2026-10-19&days=7").get_json()
        assert [(r["date"], r["kind"], r["choice"]) for r in rows] == [
            ("2026-10-19", "school", "Pizza"), ("2026-10-20", "school", "Tacos"),
        ]


# ---------------------------------------------------------------------------
# Adding recipe ingredients to a list
# ---------------------------------------------------------------------------


def _week(uid):
    fajitas = make_recipe(name="Chicken fajitas", servings=4, created_by=uid, ingredients=[
        ("Chicken breast", 1, "lb"), ("Bell peppers", 2, ""), ("Salt", None, ""),
    ])
    lemon = make_recipe(name="Lemon chicken", servings=4, created_by=uid, ingredients=[
        ("chicken breasts", 16, "oz"), ("Lemons", 2, ""),
    ])
    meals = [
        make_meal("2026-10-12", title="Chicken fajitas", recipe_id=fajitas, servings=8),
        make_meal("2026-10-13", title="Leftover fajitas", kind="leftovers"),
        make_meal("2026-10-14", title="Lemon chicken", recipe_id=lemon),
        make_meal("2026-10-15", title="Takeout pizza"),
    ]
    return meals


def _by_name(lines):
    return {line["name"]: line for line in lines}


class TestShoppingFromMeals:
    def test_preview_combines_scales_and_annotates(self, client, app):
        uid = make_user()
        list_id = make_list()
        meals = _week(uid)
        do_login(client)
        rv = jpost(client, "/api/shopping/preview", {"list_id": list_id, "meal_ids": meals})
        assert rv.status_code == 200
        data = rv.get_json()
        lines = _by_name(data["lines"])
        # 1 lb doubled for 8 servings + 16 oz
        assert lines["Chicken breast"]["quantity"] == "3 lb"
        assert lines["Chicken breast"]["notes"] == "for Chicken fajitas, Lemon chicken"
        assert lines["Bell peppers"]["quantity"] == "4"
        assert lines["Salt"]["quantity"] is None and lines["Salt"]["unquantified"]
        assert all(line["status"] == "new" and line["action"] == "add" for line in data["lines"])
        assert data["meals_without_recipe"] == ["Takeout pizza"]
        assert data["meals_skipped"] == ["Leftover fajitas"]
        assert db_count(app, "SELECT COUNT(*) FROM items") == 0  # preview never writes

    def test_preview_matches_existing_items(self, client, app):
        uid = make_user()
        list_id = make_list()
        meals = _week(uid)
        make_item(list_id, name="Chicken Breasts")  # unbought, no quantity
        make_item(list_id, name="lemon", is_bought=1)
        do_login(client)
        lines = _by_name(jpost(client, "/api/shopping/preview", {"list_id": list_id, "meal_ids": meals})
                         .get_json()["lines"])
        assert lines["Chicken breast"]["status"] == "on_list"
        assert lines["Chicken breast"]["action"] == "combine"
        assert lines["Chicken breast"]["combined_quantity"] == "3 lb"
        assert lines["Lemons"]["status"] == "bought"
        assert lines["Lemons"]["action"] == "skip"

    def test_preview_single_recipe_with_servings(self, client):
        uid = make_user()
        list_id = make_list()
        rid = make_recipe(name="Tacos", servings=4, ingredients=[("Ground beef", 1, "lb")], created_by=uid)
        do_login(client)
        lines = jpost(client, "/api/shopping/preview",
                      {"list_id": list_id, "recipe_id": rid, "servings": 8}).get_json()["lines"]
        assert [(line["name"], line["quantity"], line["notes"]) for line in lines] == [
            ("Ground beef", "2 lb", "for Tacos"),
        ]

    def test_preview_validation(self, client):
        make_user()
        list_id = make_list()
        do_login(client)
        assert jpost(client, "/api/shopping/preview", {"list_id": 999, "meal_ids": [1]}).status_code == 404
        assert jpost(client, "/api/shopping/preview", {"list_id": list_id, "meal_ids": []}).status_code == 400
        assert jpost(client, "/api/shopping/preview", {"list_id": list_id, "meal_ids": [999]}).status_code == 404
        assert jpost(client, "/api/shopping/preview", {"list_id": list_id, "recipe_id": 999}).status_code == 404

    def test_apply_adds_combines_skips_and_marks_meals(self, client, app):
        uid = make_user()
        list_id = make_list()
        meals = _week(uid)
        beef = make_item(list_id, name="Ground beef")
        with app.app_context():
            db = get_db()
            db.execute("UPDATE items SET quantity = '1 lb', notes = 'lean' WHERE id = ?", (beef,))
            db.commit()
        do_login(client)
        rv = jpost(client, "/api/shopping/apply", {
            "list_id": list_id, "section": "now", "meal_ids": meals[:1],
            "lines": [
                {"name": "Bell peppers", "quantity": "4", "notes": "for Chicken fajitas", "action": "add"},
                {"name": "Ground beef", "quantity": "2 lb", "notes": "for Tacos", "action": "combine",
                 "existing_item_id": beef},
                {"name": "Salt", "action": "skip"},
            ],
        })
        assert rv.status_code == 201
        assert rv.get_json() == {"added": 1, "combined": 1}
        rows = db_all(app, "SELECT name, quantity, notes, section FROM items ORDER BY id")
        assert [tuple(r) for r in rows] == [
            ("Ground beef", "3 lb", "lean; for Tacos", "now"),
            ("Bell peppers", "4", "for Chicken fajitas", "now"),
        ]
        marked = db_query(app, "SELECT added_to_list_id, added_to_list_at FROM planned_meals WHERE id = ?",
                          (meals[0],))
        assert marked["added_to_list_id"] == list_id and marked["added_to_list_at"]
        assert db_count(app, "SELECT COUNT(*) FROM audit_log WHERE action = 'meals.add_to_list'") == 1

    def test_apply_combine_keeps_unparseable_amounts_in_notes(self, client, app):
        make_user()
        list_id = make_list()
        item = make_item(list_id, name="Tomatoes")
        with app.app_context():
            db = get_db()
            db.execute("UPDATE items SET quantity = 'a few' WHERE id = ?", (item,))
            db.commit()
        do_login(client)
        rv = jpost(client, "/api/shopping/apply", {"list_id": list_id, "lines": [
            {"name": "Tomatoes", "quantity": "2 cans", "notes": "for Chili", "action": "combine",
             "existing_item_id": item},
        ]})
        assert rv.status_code == 201
        row = db_query(app, "SELECT quantity, notes FROM items WHERE id = ?", (item,))
        assert (row["quantity"], row["notes"]) == ("a few", "+2 cans for Chili")

    def test_apply_is_all_or_nothing(self, client, app):
        make_user()
        list_id = make_list()
        other_list = make_list("Hardware")
        foreign = make_item(other_list, name="Nails")
        do_login(client)
        rv = jpost(client, "/api/shopping/apply", {"list_id": list_id, "lines": [
            {"name": "Peppers", "action": "add"},
            {"name": "Nails", "action": "combine", "existing_item_id": foreign},
        ]})
        assert rv.status_code == 400
        assert db_count(app, "SELECT COUNT(*) FROM items WHERE list_id = ?", (list_id,)) == 0

    @pytest.mark.parametrize("line", [
        {"name": "", "action": "add"},
        {"name": "x" * 201, "action": "add"},
        {"name": "Peppers", "quantity": "x" * 51, "action": "add"},
        {"name": "Peppers", "action": "buy"},
        "Peppers",
    ])
    def test_apply_rejects_invalid_lines(self, client, app, line):
        make_user()
        list_id = make_list()
        do_login(client)
        rv = jpost(client, "/api/shopping/apply", {"list_id": list_id, "lines": [line]})
        assert rv.status_code == 400
        assert db_count(app, "SELECT COUNT(*) FROM items") == 0

    def test_apply_requires_auth_and_account(self, client):
        list_id = make_list()
        body = {"list_id": list_id, "lines": [{"name": "Peppers", "action": "add"}]}
        assert jpost(client, "/api/shopping/apply", body).status_code == 401
        make_user()
        do_login(client)
        client.environ_base["HTTP_X_ACCOUNT_ID"] = "someone-else"
        assert jpost(client, "/api/shopping/apply", body).status_code == 409


# ---------------------------------------------------------------------------
# Recipe URL import
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for requests.Response, used to avoid real network calls."""

    def __init__(self, status_code=200, headers=None, chunks=None, encoding="utf-8"):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []
        self.encoding = encoding
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


def _fake_getaddrinfo(host, *args, **kwargs):
    """Resolves loopback names normally, everything else to a fixed public IP.

    Keeps SSRF-guard tests hermetic (no real DNS lookups) while still letting
    the redirect-to-a-private-address test exercise the real loopback check.
    """
    if host in ("127.0.0.1", "localhost"):
        return [(2, 1, 6, "", ("127.0.0.1", 0))]
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


class TestRecipeUrlImport:
    def test_request_goes_to_the_vetted_ip_with_real_host(self):
        import app as app_module
        page = _FakeResponse(
            status_code=200,
            headers={"Content-Type": "text/html"},
            chunks=[b"<html>ok</html>"],
        )
        with patch("app.socket.getaddrinfo", side_effect=_fake_getaddrinfo), \
             patch("app.requests.Session.get", autospec=True, return_value=page) as mock_get:
            app_module._fetch_recipe_page("https://Example.com/recipes/1?x=2#top")
        session, url = mock_get.call_args.args
        assert url == "https://93.184.216.34/recipes/1?x=2"
        assert mock_get.call_args.kwargs["headers"]["Host"] == "example.com"
        assert session.trust_env is False

    def test_ipv4_mapped_loopback_rejected(self):
        import app as app_module
        with patch("app.socket.getaddrinfo", return_value=[(10, 1, 6, "", ("::ffff:127.0.0.1", 0, 0, 0))]):
            assert app_module._resolve_safe_ip("sneaky.example") is None

    def test_requires_auth(self, client):
        rv = jpost(client, "/api/recipes/extract-url", {"url": "http://example.com/recipe"})
        assert rv.status_code == 401

    def test_not_configured_returns_501(self, client):
        make_user()
        do_login(client)
        assert config.ANTHROPIC_API_KEY is None
        rv = jpost(client, "/api/recipes/extract-url", {"url": "http://example.com/recipe"})
        assert rv.status_code == 501

    def test_rejects_literal_private_ip(self, client, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        with patch("app.requests.Session.get") as mock_get:
            rv = jpost(client, "/api/recipes/extract-url", {"url": "http://127.0.0.1/recipe"})
        assert rv.status_code == 400
        mock_get.assert_not_called()

    def test_rejects_localhost_hostname(self, client, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        with patch("app.requests.Session.get") as mock_get:
            rv = jpost(client, "/api/recipes/extract-url", {"url": "http://localhost/recipe"})
        assert rv.status_code == 400
        mock_get.assert_not_called()

    def test_rejects_shared_address_space(self, client, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        with patch("app.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("100.64.0.1", 0))
        ]), patch("app.requests.Session.get") as mock_get:
            rv = jpost(client, "/api/recipes/extract-url", {"url": "http://internal.example/"})
        assert rv.status_code == 400
        mock_get.assert_not_called()

    def test_malformed_port_is_rejected(self, client, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        rv = jpost(client, "/api/recipes/extract-url", {"url": "http://example.com:notaport/"})
        assert rv.status_code == 400

    def test_happy_path_extracts_without_saving(self, client, app, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        page = _FakeResponse(
            status_code=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            chunks=[b"<html><body><h1>Pancakes</h1></body></html>"],
        )
        extracted = {
            "name": "Pancakes", "notes": "Fluffy", "servings": 6,
            "ingredients": ["2 cups flour", "1 egg"], "steps": ["Mix", "Cook"],
        }
        block = MagicMock(type="text", text=json.dumps(extracted))
        fake_client = MagicMock()
        fake_client.messages.create.return_value = MagicMock(content=[block])
        with patch("app.socket.getaddrinfo", side_effect=_fake_getaddrinfo), \
             patch("app.requests.Session.get", return_value=page), \
             patch("app.anthropic.Anthropic", return_value=fake_client):
            rv = jpost(client, "/api/recipes/extract-url", {"url": "http://example.com/recipe"})
        assert rv.status_code == 200
        data = rv.get_json()
        assert data["name"] == "Pancakes"
        assert data["ingredients"] == [
            {"name": "flour", "qty": 2.0, "unit": "cup", "note": ""},
            {"name": "egg", "qty": 1.0, "unit": "", "note": ""},
        ]
        assert data["servings"] == 6
        assert data["source_url"] == "http://example.com/recipe"
        assert data["steps"] == ["Mix", "Cook"]
        assert db_count(app, "SELECT COUNT(*) FROM recipes") == 0

    def test_redirect_to_private_ip_rejected(self, client, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        redirect_resp = _FakeResponse(status_code=302, headers={"Location": "http://127.0.0.1/internal"})
        with patch("app.socket.getaddrinfo", side_effect=_fake_getaddrinfo), \
             patch("app.requests.Session.get", return_value=redirect_resp) as mock_get:
            rv = jpost(client, "/api/recipes/extract-url", {"url": "http://example.com/recipe"})
        assert rv.status_code == 502
        mock_get.assert_called_once()

    def test_non_html_response_rejected(self, client, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        page = _FakeResponse(status_code=200, headers={"Content-Type": "application/pdf"}, chunks=[b"%PDF"])
        with patch("app.socket.getaddrinfo", side_effect=_fake_getaddrinfo), \
             patch("app.requests.Session.get", return_value=page):
            rv = jpost(client, "/api/recipes/extract-url", {"url": "http://example.com/recipe.pdf"})
        assert rv.status_code == 502

    def test_oversized_response_rejected(self, client, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        big_chunk = b"x" * (3 * 1024 * 1024)
        page = _FakeResponse(status_code=200, headers={"Content-Type": "text/html"}, chunks=[big_chunk])
        with patch("app.socket.getaddrinfo", side_effect=_fake_getaddrinfo), \
             patch("app.requests.Session.get", return_value=page):
            rv = jpost(client, "/api/recipes/extract-url", {"url": "http://example.com/recipe"})
        assert rv.status_code == 502

    def test_llm_reports_not_a_recipe(self, client, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        page = _FakeResponse(status_code=200, headers={"Content-Type": "text/html"},
                              chunks=[b"<html>not a recipe</html>"])
        block = MagicMock(type="text", text=json.dumps({"error": "not_a_recipe"}))
        fake_client = MagicMock()
        fake_client.messages.create.return_value = MagicMock(content=[block])
        with patch("app.socket.getaddrinfo", side_effect=_fake_getaddrinfo), \
             patch("app.requests.Session.get", return_value=page), \
             patch("app.anthropic.Anthropic", return_value=fake_client):
            rv = jpost(client, "/api/recipes/extract-url", {"url": "http://example.com/page"})
        assert rv.status_code == 422

    def test_malformed_llm_output_returns_422(self, client, monkeypatch):
        make_user()
        do_login(client)
        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "fake-key")
        page = _FakeResponse(status_code=200, headers={"Content-Type": "text/html"},
                              chunks=[b"<html>recipe</html>"])
        block = MagicMock(type="text", text="not json at all")
        fake_client = MagicMock()
        fake_client.messages.create.return_value = MagicMock(content=[block])
        with patch("app.socket.getaddrinfo", side_effect=_fake_getaddrinfo), \
             patch("app.requests.Session.get", return_value=page), \
             patch("app.anthropic.Anthropic", return_value=fake_client):
            rv = jpost(client, "/api/recipes/extract-url", {"url": "http://example.com/page"})
        assert rv.status_code == 422


class TestLunchReviewRegressions:
    def test_missing_kind_does_not_clear_saved_lunch(self, client):
        make_user()
        do_login(client)
        kid = jpost(client, '/api/kids', {'name': 'Ava'}).get_json()['id']
        payload = {'kid_id': kid, 'date': '2026-10-12', 'kind': 'home', 'choice': 'Sandwich'}
        assert jput(client, '/api/lunches', payload).status_code == 200
        assert jput(client, '/api/lunches', {'kid_id': kid, 'date': payload['date']}).status_code == 400
        assert client.get('/api/lunches?start=2026-10-12').get_json()[0]['choice'] == 'Sandwich'

    @pytest.mark.parametrize('day', ['2026-10-17', '2026-10-18'])
    def test_rejects_invisible_weekend_lunches(self, client, day):
        make_user()
        do_login(client)
        kid = jpost(client, '/api/kids', {'name': 'Ava'}).get_json()['id']
        assert jput(client, '/api/lunches', {'kid_id': kid, 'date': day, 'kind': 'home'}).status_code == 400
        assert client.get('/api/lunches?start=2026-10-12').get_json() == []

    def test_copy_normalizes_week_and_preserves_weekdays(self, client):
        make_user()
        do_login(client)
        kid = jpost(client, '/api/kids', {'name': 'Ava'}).get_json()['id']
        jput(client, '/api/lunches', {'kid_id': kid, 'date': '2026-10-12', 'kind': 'school'})
        rv = jpost(client, '/api/lunches/copy-week', {'from': '2026-10-14', 'to': '2026-10-23'})
        assert rv.get_json() == {'copied': 1}
        assert client.get('/api/lunches?start=2026-10-19').get_json() == [
            {'kid_id': kid, 'date': '2026-10-19', 'kind': 'school', 'choice': None}]
        assert jpost(client, '/api/lunches/copy-week', {'from': '2026-10-12', 'to': '2026-10-16'}).status_code == 400

    @pytest.mark.parametrize('endpoint', ['meals', 'lunches'])
    def test_date_range_overflow_is_client_error(self, client, endpoint):
        make_user()
        do_login(client)
        assert client.get(f'/api/{endpoint}?start=9999-12-31&days=7').status_code == 400
        assert client.get(f'/api/{endpoint}?start=9999-12-31&days=1').status_code == 200


@pytest.mark.parametrize('method,path', [
    ('get', '/api/kids'), ('post', '/api/kids'),
    ('put', '/api/kids/1'), ('delete', '/api/kids/1'),
    ('get', '/api/lunches?start=2026-10-12'), ('put', '/api/lunches'),
    ('post', '/api/lunches/copy-week'),
])
def test_lunch_endpoints_require_authentication(client, method, path):
    assert getattr(client, method)(path, json={}).status_code == 401


@pytest.mark.parametrize('method,path', [
    ('post', '/api/kids'), ('put', '/api/kids/1'), ('delete', '/api/kids/1'),
    ('put', '/api/lunches'), ('post', '/api/lunches/copy-week'),
])
def test_lunch_writes_require_csrf(client, app, method, path):
    make_user()
    do_login(client)
    with patch.dict(app.config, WTF_CSRF_ENABLED=True):
        rv = getattr(client, method)(path, json={})
    assert rv.status_code == 400
    assert rv.get_json()['error'] == 'csrf'
