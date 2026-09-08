import sqlite3

import pytest

import config
from app import get_db, upgrade_db
from conftest import do_login, make_list, make_user


def test_deleted_cookie_cannot_authenticate_reused_id(app, client, monkeypatch):
    make_user('admin', role='admin')
    deleted = make_user('former')
    former = app.test_client()
    do_login(former, 'former')
    do_login(client, 'admin')
    # Exercise deletion with existing foreign-key references too.
    make_list('Former list', created_by=deleted)
    former.post('/api/items/clear-bought', json={})
    token = client.get('/api/csrf-token').json['token']
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    assert client.post(f'/admin/users/{deleted}/delete', data={'csrf_token': token}).status_code == 302
    assert make_user('replacement') == deleted
    assert former.get('/api/lists').status_code == 401
    assert former.get('/admin/users').location.endswith('/login')


def test_legacy_cookie_must_sign_in_again(client):
    make_user()
    do_login(client)
    with client.session_transaction() as session:
        del session['account_id']
    assert client.get('/api/lists').status_code == 401


def test_legacy_database_identity_migration(tmp_path, monkeypatch):
    path = str(tmp_path / 'legacy.db')
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT);
            CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);
            INSERT INTO users VALUES (1, 'legacy', 'hash');
        ''')
    monkeypatch.setattr(config, 'DATABASE', path)
    upgrade_db()
    with sqlite3.connect(path) as db:
        original = db.execute('SELECT account_id FROM users').fetchone()[0]
        assert len(original) == 32
    upgrade_db()
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT account_id FROM users').fetchone()[0] == original
        db.execute('DELETE FROM users')
        db.execute("INSERT INTO users (username, password_hash) VALUES ('replacement', 'hash')")
        row_id, identity = db.execute('SELECT id, account_id FROM users').fetchone()
        assert row_id == 1
        assert len(identity) == 32 and identity != original


@pytest.mark.parametrize('role', ['user', 'admin'])
def test_forced_password_change_blocks_api_and_admin(app, client, role):
    make_user(role=role, must_change=1)
    do_login(client)
    assert client.get('/api/lists').json['error'] == 'password_change_required'
    assert client.post('/api/lists', json={'name': 'blocked'}).status_code == 403
    assert client.get('/admin/users').location.endswith('/change-password')
    assert client.post('/admin/users/create', data={'username': 'blocked', 'password': 'pass1234'}).location.endswith('/change-password')
    assert client.get('/change-password').status_code == 200
    assert client.get('/api/csrf-token').status_code == 200
    assert client.post('/change-password', data={'password': 'newpass123', 'confirm': 'newpass123'}).status_code == 302
    assert client.post('/api/lists', json={'name': 'allowed'}).status_code == 201


def test_old_tab_write_rejected_after_account_switch(app, client, monkeypatch):
    make_user('alice')
    make_user('bob')
    do_login(client, 'alice')
    old_account = client.environ_base['HTTP_X_ACCOUNT_ID']
    do_login(client, 'bob')
    token = client.get('/api/csrf-token').json['token']
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
    response = client.post('/api/lists', json={'name': 'blocked'}, headers={
        'X-Account-ID': old_account, 'X-CSRFToken': token,
    })
    assert response.status_code == 409
    assert response.json['error'] == 'account_changed'
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM lists WHERE name = 'blocked'").fetchone()[0] == 0


def test_legacy_offline_write_without_account_is_rejected(client):
    make_user()
    do_login(client)
    del client.environ_base['HTTP_X_ACCOUNT_ID']
    assert client.post('/api/lists', json={'name': 'blocked'}).status_code == 409


def test_private_responses_bypass_http_cache(client):
    make_user()
    do_login(client)
    for path in ('/', '/api/lists', '/api/csrf-token'):
        response = client.get(path)
        assert response.headers['Cache-Control'] == 'no-store'
        assert response.headers['X-Account-ID'] == client.environ_base['HTTP_X_ACCOUNT_ID']
    assert client.post('/logout').headers['Cache-Control'] == 'no-store'
