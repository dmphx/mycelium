"""Exact file selection and native-owner isolation without provider access."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.webplayer import web_player as wp


def file(name, fid=0, size=1):
    return {"name": name, "id": fid, "size": size}


@pytest.mark.parametrize("name", ["Show.S01E02.mkv", "Show.1x02.mp4", "Season 01/Show.S01E02.mkv", "Show.s1e2.MKV"])
def test_episode_selects_exact_coordinates_instead_of_largest(name):
    wanted = file(name)
    assert wp._select_file([file("Show.S01E01.mkv", 1, 10000), wanted], "tv", 1, 2) is wanted


@pytest.mark.parametrize("name", ["Show.S01E01.mkv", "Show.S02E02.mkv", "02.mkv", "Show.E02.mkv",
    "Show.S01E02E03.mkv", "Show.S01E02-E03.mkv", "Show.S01E02-03.mkv", "Show.1x02-1x03.mkv",
    "Show.S01E02.S01E03.mkv", "Show.S01E02.txt", "samples/Show.S01E02.mkv", "Show.S01E02.sample.mkv",
    "samples\\Show.S01E02.mkv", "Show.1x02x03.mkv", "Show.S01E02+03.mkv", "Show.S01E02&03.mkv"])
def test_unqualified_episode_does_not_fall_back(name):
    assert wp._select_file([file(name)], "tv", 1, 2) is None


def test_specials_are_explicit_and_duplicate_encodings_fail_closed():
    special = file("Show.S00E01.mkv")
    assert wp._select_file([special], "tv", 0, 1) is special
    assert wp._select_file([special], "tv", 1, 1) is None
    assert wp._select_file([file("Show.S01E01.mkv"), file("Show.S01E01.mp4", 1)], "tv", 1, 1) is None


def test_movie_requires_one_main_file_and_never_uses_nonvideo_or_largest_fallback():
    main = file("Film.2026.mkv", 0, 1)
    assert wp._select_file([main, file("Film.trailer.mp4", 1, 1000)], "movie") is main
    for files in ([main, file("Other Film.mp4", 1, 1000)], [file("readme.txt")],
                  [file("Show.S01E01.mp4")], [file("Film.sample.mp4")]):
        assert wp._select_file(files, "movie") is None


@pytest.mark.parametrize("files", [None, {}, [None], [{"name": "Film.mp4"}], [file("Film.mp4", True)],
    [file("Film.mp4", -1)], [file("Film.mp4", "0")], [file(None)], [file("bad\x00.mp4")],
    [file("Film.mp4"), file("trailer.mp4")]])
def test_malformed_file_inventory_fails_closed(files):
    assert wp._select_file(files, "movie") is None


@pytest.mark.parametrize("args", [ ("tt1234567", "tv", None, None), ("tt1234567", "tv", True, 1),
    ("tt1234567", "tv", 1, 0), ("tt1234567", "tv", -1, 1), ("tt1234567", "tv", 1, "2"),
    ("tt1234567", "movie", 1, 1), ("bad", "movie", None, None), ("tt1234567", "series", 1, 1)])
def test_invalid_target_is_rejected_before_provider_or_job_creation(args, monkeypatch):
    owner = Mock(side_effect=AssertionError("owner/provider must not be reached"))
    monkeypatch.setattr(wp, "_owner_enabled", owner)
    with pytest.raises(ValueError):
        wp.start_prepare_job(*args, owner_id=1)
    owner.assert_not_called()


def test_cdn_resolution_requests_exact_provider_file_id_including_zero(monkeypatch):
    files = [file("Show.S01E01.mkv", 5, 1000), file("Show.S01E02.mkv", 0, 1)]
    monkeypatch.setattr(wp.torbox, "find_by_hash", lambda h: {"id": 42, "files": files})
    monkeypatch.setattr(wp.torbox, "_is_ready", lambda item: True)
    request = Mock(return_value="https://synthetic.invalid/media")
    monkeypatch.setattr(wp, "_request_dl", request)
    stream = SimpleNamespace(info_hash="a" * 40)
    assert wp._get_cdn_url(stream, "tv", 1, 2) == ("https://synthetic.invalid/media", 42, 0)
    request.assert_called_once_with(42, 0)
    request.reset_mock()
    assert wp._get_cdn_url(stream, "tv", 1, 3) == (None, None, None)
    request.assert_not_called()


def test_actual_native_auth_csrf_routes_position_and_independent_jobs(tmp_path):
    # A clean interpreter avoids the legacy suite's collection-time db mocks.
    script = r'''
import json, math, socket, threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

def denied(*args, **kwargs):
    raise AssertionError('unexpected provider/network access')
socket.socket.connect = denied
socket.create_connection = denied
socket.getaddrinfo = denied
before_threads = set(threading.enumerate())
import db
db.init()
import plugins.webplayer as plugin
from plugins.webplayer import web_player as wp, routes, positions
assert set(threading.enumerate()) == before_threads, 'import started a hardware/provider worker'
plugin.run_migrations()
from flask import Flask, jsonify
from flask_wtf.csrf import CSRFProtect, generate_csrf
app = Flask(__name__)
app.config.update(TESTING=True, SECRET_KEY='synthetic-player-tests-only')
CSRFProtect(app)
app.register_blueprint(routes.bp)
@app.get('/synthetic-csrf')
def csrf(): return jsonify(token=generate_csrf())
uids = [db.create_user('synthetic-player-'+str(i), 'synthetic') for i in range(3)]
with db._connect() as conn:
    conn.execute('UPDATE users SET webplayer_enabled=1 WHERE id IN (?,?)', uids[:2])
def client(uid=None):
    c = app.test_client()
    if uid:
        with c.session_transaction() as s:
            s.update(user_id=uid, user=db.get_user(uid)['username'], role='user')
    headers={'X-CSRFToken':c.get('/synthetic-csrf').json['token']}
    return c, headers
a, ah = client(uids[0]); b, bh = client(uids[1]); disabled, dh = client(uids[2]); anon, nh = client()
class DormantThread:
    def __init__(self, *args, **kwargs): pass
    def start(self): pass
wp.threading = SimpleNamespace(Thread=DormantThread)
wp.DirectSession.start_heartbeat = lambda self: None
wp.HLSSession.start_heartbeat = lambda self: None
wp.PLAYER_TMP_DIR = Path('synthetic-player')
target={'imdb_id':'tt1234567','media_type':'tv','season':1,'episode':2}
assert a.post('/ui/api/web-player/prepare', json=target).status_code == 400
assert disabled.post('/ui/api/web-player/prepare', json=target, headers=dh).status_code == 403
assert anon.post('/ui/api/web-player/prepare', json=target, headers=nh).status_code == 403
job_ids=[]
for c,h in [(a,ah),(a,ah),(b,bh)]:
    response=c.post('/ui/api/web-player/prepare', json=target, headers=h)
    assert response.status_code==200, response.status_code
    job_ids.append(response.json['job_id'])
assert len(set(job_ids))==3 and all(len(j)==32 for j in job_ids)
assert a.post('/ui/api/web-player/prepare',json=target,headers=ah).status_code==503
assert len(wp._jobs)==3
candidate=SimpleNamespace(info_hash='a'*40, name='Synthetic H264 AAC 1080p', title='Synthetic',quality='1080p')
wp.find_web_candidates=lambda *a,**k:[candidate]
wp.torbox.find_by_hash=lambda h:{'id':42}
wp._get_cdn_url=lambda *a,**k:('https://synthetic.invalid/never-requested',42,0)
for jid in job_ids:
    wp._run_job(wp._jobs[jid])
    assert wp._jobs[jid].status==wp.JobStatus.READY
assert set(wp._direct_sessions)==set(job_ids), 'hash-shared playback leaked across jobs'
for jid in job_ids[:2]:
    session=wp._direct_sessions[jid]
    assert session.owner_id==uids[0] and session.content_hash=='a'*40 and session.target==('tt1234567','tv',1,2)
token=job_ids[0]
own=a.get('/ui/api/web-player/status/'+token)
assert own.status_code==200 and 'cdn_url' not in own.json
assert b.get('/ui/api/web-player/status/'+token).status_code==404
assert a.get('/ui/api/web-player/status/'+job_ids[2]).status_code==404
generation='b'*32
root=wp.PLAYER_TMP_DIR/token/generation;root.mkdir(parents=True)
(root/'playlist.m3u8').write_text('#EXTM3U\nseg00000.ts\n')
(root/'seg00000.ts').write_bytes(b'synthetic segment')
(root/'sub_0_eng.vtt').write_text('WEBVTT\n')
(root/'hls_ready.txt').write_text('playlist.m3u8')
proc=SimpleNamespace(poll=lambda:None,terminate=Mock(side_effect=AssertionError('foreign seek reached process')))
wp._sessions[token]=wp.HLSSession(token=token,owner_id=uids[0],content_hash='a'*40,proc=proc,tmp_dir=root)
wp._generations[(token,generation)]=wp._sessions[token]
for path in ['/direct','/hls-status','/subtitles','/subtitles/'+generation+'/sub_0_eng.vtt','/hls/'+generation+'/playlist.m3u8','/hls/'+generation+'/seg00000.ts']:
    assert b.get('/stream/'+token+path).status_code==404, path
    assert a.get('/stream/unknown'+path).status_code==404, path
for path,body in [('/convert-hls',{}),('/seek',{'position_s':10}),('/position',{'position_s':10})]:
    assert b.post('/stream/'+token+path,json=body,headers=bh).status_code==404, path
    assert a.post('/stream/unknown'+path,json=body,headers=ah).status_code==404, path
    assert a.post('/stream/'+token+path,json=body).status_code==400, path
assert a.get('/stream/'+token+'/hls/'+generation+'/playlist.m3u8').get_data(as_text=True).startswith('#EXTM3U')
assert a.get('/stream/'+token+'/subtitles/'+generation+'/sub_0_eng.vtt').status_code==200
outside=Path('synthetic-outside.ts');outside.write_bytes(b'must not serve')
(root/'seg99999.ts').symlink_to(outside.resolve())
assert a.get('/stream/'+token+'/hls/'+generation+'/seg99999.ts').status_code==404
assert a.get('/stream/'+token+'/hls/'+generation+'/ffmpeg.log').status_code==400
(root/'hls_error.txt').write_text('secret synthetic provider URL must not escape')
assert 'secret' not in a.get('/stream/'+token+'/hls-status').get_data(as_text=True)
(root/'hls_error.txt').unlink()
(root/'hls_ready.txt').write_text('../outside.m3u8')
assert a.get('/stream/'+token+'/hls-status').json['status']=='ready'  # Untrusted marker ignored.
wp._sessions.pop(token)
wp._direct_sessions[token].conversion_error='secret synthetic provider URL must not escape'
error=a.get('/stream/'+token+'/hls-status')
assert error.json['status']=='error' and 'secret' not in error.get_data(as_text=True)
wp._sessions[token]=wp._generations[(token,generation)]
for bad in [True, '1', -1, float('nan'), float('inf'), None, 604801]:
    assert a.post('/stream/'+token+'/seek',json={'position_s':bad},headers=ah).status_code==400
    assert a.post('/stream/'+token+'/position',json={'position_s':bad,'generation':generation},headers=ah).status_code==400
assert a.post('/stream/'+token+'/position',json={'position_s':12,'duration_s':100,'generation':generation},headers=ah).status_code==200
assert positions.read(wp._direct_sessions[token])=={'position_s':12.0,'duration_s':100.0}
for stale in [None, 'c'*32, '../outside']:
    assert a.post('/stream/'+token+'/position',json={'position_s':50,'duration_s':100,'generation':stale},headers=ah).status_code==409
wp._direct_sessions[token].operation_lock.acquire()
try:
    assert a.post('/stream/'+token+'/position',json={'position_s':50,'duration_s':100,'generation':generation},headers=ah).status_code==409
finally:
    wp._direct_sessions[token].operation_lock.release()
assert positions.read(wp._direct_sessions[token])=={'position_s':12.0,'duration_s':100.0}
# Same exact owner/file can resume in another independent session. Other owner/file/episode cannot.
assert positions.read(wp._direct_sessions[job_ids[1]])['position_s']==12
assert positions.read(wp._direct_sessions[job_ids[2]]) is None
saved=wp._direct_sessions[job_ids[1]]
saved.file_id=1;assert positions.read(saved) is None
saved.file_id=0;saved.target=('tt1234567','tv',1,3);assert positions.read(saved) is None
assert a.post('/stream/'+token+'/position',json={'position_s':101,'duration_s':100,'generation':generation},headers=ah).status_code==400
# Native policy changes take effect even with an old authenticated session and known token.
with db._connect() as conn: conn.execute('UPDATE users SET webplayer_enabled=0 WHERE id=?',(uids[0],))
assert a.get('/stream/'+token+'/direct').status_code==403
assert a.get('/ui/api/web-player/status/'+token).status_code==403
try: positions.save(wp._direct_sessions[token],13,100)
except PermissionError: pass
else: raise AssertionError('position write ignored revoked feature')
with db._connect() as conn: conn.execute('UPDATE users SET enabled=0 WHERE id=?',(uids[1],))
assert b.get('/ui/api/web-player/status/'+job_ids[2]).status_code==403
'''
    env = dict(os.environ, DB_PATH=str(tmp_path / "native.db"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path,
                            env=dict(env, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
