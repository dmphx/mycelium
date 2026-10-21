"""Self-service plugin fields must never inherit an administrator-only field."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import plugin_loader


def install(monkeypatch, *metas):
    monkeypatch.setattr(plugin_loader, "_loaded", {
        str(index): {"meta": meta} for index, meta in enumerate(metas)
    })


def test_administrative_fields_win_across_plugins(monkeypatch):
    install(monkeypatch,
            {"user_fields": ["synthetic_preference", "webplayer_enabled"]},
            {"admin_fields": ["webplayer_enabled"]})
    assert plugin_loader.self_editable_user_fields() == ["synthetic_preference"]
    # Preserve the existing administrator form's union.
    assert "webplayer_enabled" in plugin_loader.user_fields()


@pytest.mark.parametrize("field", [
    "id", "user_id", "username", "password_hash", "role", "quota_monthly",
    "auto_approve", "enabled", "region", "created_at", "last_login",
    "library_click_jellyfin", "discover_language_include", "discover_language_exclude",
])
def test_core_fields_cannot_become_plugin_preferences(monkeypatch, field):
    install(monkeypatch, {"user_fields": [field, "synthetic_preference"]})
    assert plugin_loader.self_editable_user_fields() == ["synthetic_preference"]


@pytest.mark.parametrize("meta", [
    {"user_fields": "webplayer_enabled"}, {"admin_fields": "webplayer_enabled"},
    {"user_fields": [None]}, {"admin_fields": [5]},
    {"user_fields": ["Role"]}, {"admin_fields": ["bad-field"]},
    {"user_fields": ["field\n"]}, {"user_fields": ["x" * 65]},
])
def test_malformed_plugin_metadata_fails_closed(monkeypatch, meta):
    install(monkeypatch, {"user_fields": ["synthetic_preference"]}, meta)
    assert plugin_loader.self_editable_user_fields() == []


def test_real_application_self_service_policy(tmp_path):
    """Real app routing, native auth, CSRF and SQLite in an isolated process.

    The legacy suite mocks modules during collection, so the application check
    uses a clean interpreter. Every user and field is synthetic; networking is
    denied and all writable paths are inside this temporary directory.
    """
    script = r'''
import os, socket
def no_network(*args, **kwargs):
    raise AssertionError('unexpected network in isolated policy test')
socket.socket.connect = no_network
socket.create_connection = no_network
socket.getaddrinfo = no_network
import app as real
import auth, db, plugin_loader
import plugins.webplayer as webplayer
from html.parser import HTMLParser
webplayer.run_migrations()
with db._connect() as conn:
    conn.execute('ALTER TABLE users ADD COLUMN synthetic_preference INTEGER NOT NULL DEFAULT 0')
plugin_loader._loaded = {
    'webplayer': {'meta': webplayer.PLUGIN_META, 'module': webplayer},
    'synthetic': {'meta': {'user_fields': ['synthetic_preference']}}
}
uid = db.create_user('synthetic-viewer', 'fixture$disabled')
other = db.create_user('synthetic-other', 'fixture$disabled')
admin = db.create_user('synthetic-admin', 'fixture$disabled', role='admin')
real.app.config['TESTING'] = True
class Meta(HTMLParser):
    token = None
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('name') == 'csrf-token':
            self.token = attrs['content']
def client(user_id=None):
    c = real.app.test_client()
    if user_id:
        user = db.get_user(user_id)
        with c.session_transaction() as s:
            s.update(user_id=user_id, user=user['username'], role=user['role'], auth_source='password')
    if user_id:
        page = c.get('/app')
        parser = Meta(); parser.feed(page.get_data(as_text=True))
        assert parser.token
        return c, {'X-CSRFToken': parser.token}
    return c, {}
c, headers = client(uid)
path = '/ui/api/me/plugin-fields'
assert c.post(path, json={'synthetic_preference': True}).status_code == 400
assert db.get_user(uid)['synthetic_preference'] == 0
for body in [
    {'webplayer_enabled': True}, {'webplayer_enabled': False},
    {'synthetic_preference': True, 'webplayer_enabled': True},
    {'role': True}, {'enabled': False}, {'quota_monthly': 0},
    {'synthetic_preference': 1}, {'synthetic_preference': 0},
    {'synthetic_preference': 'false'}, {'synthetic_preference': None},
    {}, [], ['synthetic_preference'], True, 'synthetic_preference'
]:
    response = c.post(path, json=body, headers=headers)
    assert response.status_code == 400, (body, response.status_code)
    row = db.get_user(uid)
    assert row['synthetic_preference'] == 0 and row['webplayer_enabled'] == 0
    assert row['role'] == 'user' and row['enabled'] == 1
assert c.post(path, json={'synthetic_preference': True}, headers=headers).status_code == 200
assert db.get_user(uid)['synthetic_preference'] == 1
assert db.get_user(other)['synthetic_preference'] == 0
assert c.post(path, json={'synthetic_preference': False}, headers=headers).status_code == 200
assert db.get_user(uid)['synthetic_preference'] == 0
# Ordinary viewers cannot use the separate administrator update endpoint.
assert c.post('/ui/api/users/' + str(uid) + '/update', json={'webplayer_enabled': True}, headers=headers).status_code == 403
assert db.get_user(uid)['webplayer_enabled'] == 0
a, ah = client(admin)
assert a.post('/ui/api/users/' + str(uid) + '/update', json={'webplayer_enabled': True}, headers=ah).status_code == 200
assert db.get_user(uid)['webplayer_enabled'] == 1
# Even an administrator must use the explicit administrator endpoint for this field.
assert a.post(path, json={'webplayer_enabled': True}, headers=ah).status_code == 400
assert db.get_user(admin)['webplayer_enabled'] == 0
db.update_user(uid, enabled=0)
assert c.post(path, json={'synthetic_preference': True}, headers=headers).status_code == 401
assert db.get_user(uid)['synthetic_preference'] == 0
anonymous = real.app.test_client()
assert anonymous.get('/ui/api/session').status_code == 401
print('REAL_PLUGIN_FIELD_POLICY_PASS')
'''
    env = {key: value for key, value in os.environ.items() if key in ('PATH', 'HOME', 'TMPDIR')}
    env.update(DB_PATH=str(tmp_path / 'fixture.sqlite'), AUTH_ENABLED='true',
               AUTH_SESSION_SECRET='synthetic-policy-session-' * 3,
               TORBOX_API_KEY='synthetic', MEDIA_PATH=str(tmp_path / 'media'),
               SPORE_MEDIA_PATH=str(tmp_path / 'spore'), LITE_MODE='true',
               CATCHUP_ENABLED='false', INSECURE_ALLOW_ANON='false', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and 'REAL_PLUGIN_FIELD_POLICY_PASS' in result.stdout, \
        result.stdout[-1000:] + result.stderr[-2000:]
