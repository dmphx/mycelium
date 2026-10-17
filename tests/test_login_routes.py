"""Real /login route behaviour, in an isolated subprocess (no MagicMock pollution)."""
import os
import subprocess
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SCRIPT = textwrap.dedent('''
    import re, sys
    import app as m
    a = m.app
    a.config["WTF_CSRF_ENABLED"] = True
    import db, auth
    db.create_user("zed", auth.hash_password("pw-123456"), role="user")
    c = a.test_client()
    html = c.get("/login?next=/x").get_data(as_text=True)
    assert "Sign in" in html and "csrf_token" in html
    assert "Jellyfin" not in html            # default off
    assert c.post("/login", data={"username": "zed", "password": "pw-123456"}).status_code == 400  # CSRF
    tok = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    r = c.post("/login", data={"username": "zed", "password": "pw-123456", "csrf_token": tok, "next": "//evil.com"})
    assert r.status_code == 302 and r.headers["Location"] == "/", r.headers["Location"]
    with c.session_transaction() as s:
        assert s.get("user_id")
    # disabled user loses the session on next request
    db.update_user(db.get_user_by_username("zed")["id"], enabled=0)
    assert c.get("/ui/api/session").status_code in (401, 302)
    # limiter keyed per client via trusted proxy X-Real-IP
    codes = []
    for i in range(7):
        c2 = a.test_client()
        h = c2.get("/login").get_data(as_text=True)
        t = re.search(r'name="csrf_token" value="([^"]+)"', h).group(1)
        codes.append(c2.post("/login", data={"username": "zed", "password": "bad", "csrf_token": t},
                             environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                             headers={"X-Real-IP": "203.0.113.9"}).status_code)
    assert codes[-1] == 429 and codes[0] == 302, codes
    c3 = a.test_client(); h = c3.get("/login").get_data(as_text=True)
    t = re.search(r'name="csrf_token" value="([^"]+)"', h).group(1)
    assert c3.post("/login", data={"username": "zed", "password": "bad", "csrf_token": t},
                   environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                   headers={"X-Real-IP": "203.0.113.77"}).status_code == 302
    print("OK")
''')


def test_login_route_end_to_end(tmp_path):
    env = dict(os.environ, DB_PATH=str(tmp_path / "t.db"), AUTH_ENABLED="true",
               AUTH_SESSION_SECRET="s" * 40, TORBOX_API_KEY="x", MEDIA_PATH=str(tmp_path),
               SPORE_MEDIA_PATH=str(tmp_path), LITE_MODE="true", INSECURE_ALLOW_ANON="false",
               TRUSTED_PROXY_NETWORKS="127.0.0.0/8")
    env.pop("JELLYFIN_LOGIN_ENABLED", None)
    r = subprocess.run([sys.executable, "-c", SCRIPT], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "OK" in r.stdout, r.stdout[-800:] + r.stderr[-1500:]


def test_enabled_household_login_and_compat_boundary(tmp_path):
    script = r'''
import re
from unittest.mock import Mock
import app as m
import auth, db, jellyfin_login, seerr_compat, config
admin = db.create_user("owner", "disabled", role="admin")
member = db.create_user("member", "disabled", role="user")
db.link_external_identity(admin, "jellyfin", "1"*32, "2"*32)
db.link_external_identity(member, "jellyfin", "1"*32, "3"*32)
db.link_external_identity(admin, "proxy", "https://identity.example", "owner-subject")
db.link_external_identity(admin, "oidc", "https://identity.example/oidc/", "owner-oidc-subject")
assert db.upsert_oidc_user("renamed-owner", role="admin", issuer="https://identity.example/oidc/", subject="owner-oidc-subject") == admin
separate = db.upsert_oidc_user("owner", role="user", issuer="https://identity.example/oidc/", subject="different-subject")
assert separate != admin and db.get_user(admin)["role"] == "admin"
db.update_user(separate, enabled=0)
try:
    db.upsert_oidc_user("owner", role="admin", issuer="https://identity.example/oidc/", subject="different-subject")
    raise AssertionError("disabled OIDC account accepted")
except ValueError:
    pass

db.add_favorite_actor(member, 12, 'Pending actor')
db.add_favorite_actor(admin, 13, 'Approved actor')
assert [a['person_id'] for a in db.get_all_favorite_actors()]==[13]
import mdblist
assert mdblist.sync_auto_request({**db.get_user(member),'mdblist_api_key':'synthetic-key','mdblist_list_ids':'1'})==0
from plugins.trakt import trakt_api
assert trakt_api.auto_request_watchlist(member, 'synthetic-token')==0

logouts=[]
def fake_post(url, **kwargs):
    if url.endswith('/Sessions/Logout'):
        logouts.append(kwargs)
        return Mock(status_code=204)
    assert kwargs['json'] == {'Username':'member','Pw':'synthetic-password'}
    assert kwargs['allow_redirects'] is False
    return Mock(status_code=200, json=lambda: {'ServerId':'1'*32,
        'AccessToken':'synthetic-ephemeral-token', 'User':{'Id':'3'*32,'Name':'member',
        'Policy':{'IsDisabled':False,'IsAdministrator':True}}})
jellyfin_login.requests.post = fake_post
c=m.app.test_client()
html=c.get('/login',base_url='https://requests.example').get_data(as_text=True)
assert 'Sign in with Jellyfin' in html
token=re.search(r'name="csrf_token" value="([^"]+)"',html).group(1)
r=c.post('/login', base_url='https://requests.example', headers={'Referer':'https://requests.example/login'}, data={'method':'jellyfin',
    'username':'member','password':'synthetic-password','csrf_token':token})
assert r.status_code==302 and len(logouts)==1, (r.status_code,r.get_data(as_text=True))
with c.session_transaction(base_url='https://requests.example') as s:
    assert s['user_id']==member and s['role']=='user'
    assert 'synthetic-password' not in str(dict(s)) and 'synthetic-ephemeral-token' not in str(dict(s))
db.create_user_request(admin,'tt1234567',42,'movie','Owner title',status='approved')
mine=db.create_user_request(member,'tt7654321',43,'movie','Member title')
db.add_to_watchlist(admin,'tt1234567',42,'movie','Owner title')
db.add_to_watchlist(member,'tt7654321',43,'movie','Member title')
assert [x['id'] for x in c.get('/ui/api/user-requests',base_url='https://requests.example').json['items']]==[mine]
assert [x['title'] for x in c.get('/ui/api/watchlist',base_url='https://requests.example').json['items']]==['Member title']
for path in ['/ui/api/users','/ui/api/settings','/ui/api/requests/failed','/ui/api/stats','/ui/api/backups']:
    assert c.get(path,base_url='https://requests.example').status_code==403,path
headers={'X-Authentik-Username':'owner','X-Authentik-Uid':'owner-subject'}
anonymous=m.app.test_client()
assert anonymous.get('/ui/api/session',headers=headers,base_url='https://requests.example').status_code==401
assert anonymous.get('/ui/api/session',headers=headers,base_url='https://admin.example',environ_overrides={'REMOTE_ADDR':'203.0.113.8'}).status_code==401
proxied=m.app.test_client()
assert proxied.get('/ui/api/session',headers=headers,base_url='https://admin.example').json['user']['id']==admin
collision=m.app.test_client()
different={**headers,'X-Authentik-Uid':'unlinked-subject'}
who=collision.get('/ui/api/session',headers=different,base_url='https://admin.example').json['user']
assert who['id']!=admin and who['role']=='user'
assert proxied.get('/api/v1/request',base_url='https://admin.example').status_code==401
assert anonymous.get('/api/v1/unknown',headers={'Accept':'application/json'}).status_code==401
assert anonymous.get('/api/v1/status').status_code==200
kicks=[]
m._kick_off_processing=lambda *args: kicks.append(args)
m.tmdb.details=lambda *args: {'title':'Synthetic movie','imdb_id':'tt0009999'}
key={'X-Api-Key':'synthetic-api-key'}
body={'mediaType':'movie','mediaId':999}
assert anonymous.post('/api/v1/request',headers=key,json=body).status_code==201
assert anonymous.post('/api/v1/request',headers=key,json=body).status_code==200
assert len(kicks)==1
assert anonymous.post('/api/v1/request',json=body).status_code==401
db.update_user(member,enabled=0)
assert c.get('/ui/api/session',base_url='https://requests.example').status_code==401
print('OK')
'''
    env = {"PATH": os.environ.get("PATH", ""), "DB_PATH": str(tmp_path / "enabled.db"),
           "AUTH_ENABLED": "true", "AUTH_SESSION_SECRET": "s" * 40, "TORBOX_API_KEY": "test",
           "MEDIA_PATH": str(tmp_path), "SPORE_MEDIA_PATH": str(tmp_path), "LITE_MODE": "true",
           "INSECURE_ALLOW_ANON": "false", "TRUSTED_PROXY_NETWORKS": "127.0.0.1/32",
           "TRUSTED_PROXY_AUTH": "true", "TRUSTED_PROXY_AUTH_HOSTS": "admin.example",
           "TRUSTED_PROXY_USER_HEADER": "X-Authentik-Username",
           "TRUSTED_PROXY_SUBJECT_HEADER": "X-Authentik-Uid",
           "TRUSTED_PROXY_IDENTITY_NAMESPACE": "https://identity.example",
           "JELLYFIN_URL": "http://jellyfin.example", "JELLYFIN_LOGIN_ENABLED": "true",
           "JELLYFIN_AUTH_SERVER_ID": "1" * 32, "SEERR_COMPAT_ENABLED": "true",
           "SEERR_API_KEY": "synthetic-api-key", "SEERR_COMPAT_USER_ID": "1"}
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout[-500:] + result.stderr[-2000:]
