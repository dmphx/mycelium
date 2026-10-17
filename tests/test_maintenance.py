"""Persistent maintenance outcomes and the real admin/scheduler boundary."""
import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

import maintenance


@pytest.fixture()
def database(monkeypatch, tmp_path):
    saved = {m: sys.modules.get(m) for m in ("db", "config")}
    for m in saved:
        sys.modules.pop(m, None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "maintenance.db"))
    import db
    db.init()
    yield db
    db._thread_conn().close()
    for m, value in saved.items():
        sys.modules.pop(m, None)
        if value is not None:
            sys.modules[m] = value


def read(job="repair_strms"):
    scheduler = SimpleNamespace(get_job=lambda _: None)
    return next(j for j in maintenance.status(scheduler)["jobs"] if j["id"] == job)


def test_empty_status_is_read_only(database):
    before = database._thread_conn().total_changes
    assert read()["last_run"] is None
    assert read()["last_success_at"] is None
    assert database._thread_conn().total_changes == before


def test_scheduled_result_persists_and_excludes_sensitive_data(database):
    response = maintenance.run("repair_strms", lambda: {
        "scanned": 8, "relinked": 2, "url": "https://example.test/?key=secret",
        "errors": "private error", "token": "secret", "skipped": True,
    }, source="scheduled")
    assert response["run_status"] == "succeeded"
    assert read()["last_run"]["source"] == "scheduled"
    assert read()["last_run"]["result"] == {"scanned": 8, "relinked": 2}
    success = read()["last_success_at"]
    database._thread_conn().close()
    database._tls.conn = None
    maintenance.initialize()
    assert read()["last_success_at"] == success
    assert "secret" not in json.dumps(read())


@pytest.mark.parametrize("result, state", [
    ({"scanned": 0, "deferred": "maintenance_busy"}, "deferred"),
    ({"scanned": 0, "deferred": "media_unavailable"}, "deferred"),
    ({"scanned": 8, "errors": 2}, "partial"),
    ({"scanned": 8, "failed": 2}, "partial"),
])
def test_incomplete_runs_do_not_advance_last_success(database, result, state):
    maintenance.run("repair_strms", lambda: {"scanned": 8})
    success = read()["last_success_at"]
    maintenance.run("repair_strms", lambda: result)
    assert read()["last_run"]["status"] == state
    assert read()["last_success_at"] == success


def test_failure_is_saved_without_exception_secrets(database, caplog):
    def fail():
        raise RuntimeError("https://example.test/download?token=secret")
    with pytest.raises(maintenance.MaintenanceFailed) as exc:
        maintenance.run("repair_strms", fail)
    assert read()["last_run"]["status"] == "failed"
    assert read()["last_success_at"] is None
    assert "secret" not in str(exc.value) + json.dumps(read()) + caplog.text


def test_running_record_and_restart_do_not_invent_completion(database):
    def task():
        assert read()["active_run"]["status"] == "running"
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        maintenance.run("repair_strms", task)
    maintenance.initialize()
    assert read()["last_run"]["status"] == "interrupted"
    assert read()["last_run"]["finished_at"] is None
    assert read()["last_success_at"] is None
    assert read()["active_run"] is None


def test_history_is_bounded_without_losing_old_success(database):
    maintenance.run("repair_strms", lambda: {"scanned": 1})
    success = read()["last_success_at"]
    for _ in range(105):
        maintenance.run("repair_strms", lambda: {"errors": 1})
    assert read()["last_success_at"] == success
    count = database._thread_conn().execute("SELECT COUNT(*) FROM maintenance_runs").fetchone()[0]
    assert count == 101


@pytest.mark.parametrize("name", ["migrate_to_canonical_names", "cleanup_duplicate_strms", "repair_expired_strms"])
def test_busy_or_missing_library_is_not_reported_as_success(monkeypatch, tmp_path, name):
    import strm_generator as sg
    monkeypatch.setattr(sg, "MEDIA_PATH", str(tmp_path))
    lock = threading.Lock()
    monkeypatch.setattr(sg, "_maintenance_lock", lock)
    with lock:
        assert getattr(sg, name)()["deferred"] == "maintenance_busy"
    assert getattr(sg, name)()["deferred"] == "media_unavailable"


def test_real_routes_and_scheduler_use_the_same_tracker(tmp_path):
    script = r'''
from unittest.mock import Mock
import re
import app as m
import auth, db, settings
settings.set('SETUP_COMPLETE', True)
owner = db.create_user('owner', 'disabled', role='admin')
member = db.create_user('member', 'disabled', role='user')
anonymous = m.app.test_client()
assert anonymous.get('/ui/api/maintenance').status_code == 401
user = m.app.test_client()
with user.session_transaction() as s:
    s.update(user_id=member, user='member', role='user')
assert user.get('/ui/api/maintenance').status_code == 403
c = m.app.test_client()
with c.session_transaction() as s:
    s.update(user_id=owner, user='owner', role='admin')
def state(key):
    response = c.get('/ui/api/maintenance')
    assert response.headers['Cache-Control'] == 'no-store'
    return next(j for j in response.json['jobs'] if j['id'] == key)
assert state('repair_strms')['schedule']['interval_seconds'] == 21600
assert state('repair_strms')['schedule']['mode'] == 'automatic'
assert all(state(k)['schedule']['mode'] == 'manual' for k in ['migrate_canonical','cleanup_duplicates','scan_torbox'])
m.scheduler.pause_job('strm_repair')
assert state('repair_strms')['schedule']['mode'] == 'paused'
m.scheduler.resume_job('strm_repair')
assert state('repair_strms')['schedule']['next_run_at']
html = c.get('/app/').get_data(as_text=True)
token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
routes = [
 ('migrate_canonical', '/ui/api/migrate-canonical', 'migrate_to_canonical_names'),
 ('cleanup_duplicates', '/ui/api/cleanup-duplicate-strms', 'cleanup_duplicate_strms'),
 ('repair_strms', '/ui/api/repair-strms', 'repair_expired_strms'),
 ('scan_torbox', '/ui/api/torbox/scan-library', 'scan_torbox_library'),
]
for key, path, name in routes:
    task = Mock(return_value={'scanned': 4})
    setattr(m.strm_generator, name, task)
    assert c.post(path).status_code == 400
    task.assert_not_called()
    before = db._thread_conn().total_changes
    assert state(key)['last_run'] is None
    assert db._thread_conn().total_changes == before
    response = c.post(path, headers={'X-CSRFToken': token})
    assert response.status_code == 200, (path, response.get_data(as_text=True))
    task.assert_called_once()
    assert state(key)['last_run']['source'] == 'manual'
    assert state(key)['last_success_at']
m.scheduler.get_job('strm_repair').func()
assert state('repair_strms')['last_run']['source'] == 'scheduled'
saved = state('repair_strms')['last_success_at']
m.strm_generator.repair_expired_strms = Mock(side_effect=RuntimeError('private-signed-url'))
assert c.post('/ui/api/repair-strms',headers={'X-CSRFToken':token}).status_code == 500
assert state('repair_strms')['last_run']['status'] == 'failed'
assert state('repair_strms')['last_success_at'] == saved
assert 'private-signed-url' not in str(state('repair_strms'))
m.scheduler.remove_job('strm_repair')
assert state('repair_strms')['schedule']['mode'] == 'disabled'
print('OK')
'''
    env = {"PATH": os.environ.get("PATH", ""), "DB_PATH": str(tmp_path / "app.db"),
           "AUTH_ENABLED": "true", "AUTH_SESSION_SECRET": "s" * 40, "TORBOX_API_KEY": "test",
           "MEDIA_PATH": str(tmp_path), "SPORE_MEDIA_PATH": str(tmp_path), "LITE_MODE": "true",
           "INSECURE_ALLOW_ANON": "false", "CATBOX_MODE": "true"}
    result = subprocess.run([sys.executable, "-c", script], cwd=os.path.dirname(os.path.dirname(__file__)),
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "OK" in result.stdout, result.stdout[-500:] + result.stderr[-2500:]


def test_duplicate_delete_failure_is_counted(monkeypatch, tmp_path):
    import strm_generator as sg
    monkeypatch.setattr(sg, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(sg, "_maintenance_lock", threading.Lock())
    folder = tmp_path / "movies" / "Example"
    folder.mkdir(parents=True)
    (folder / "Example.strm").write_text("synthetic")
    extra = folder / "Extra.strm"
    extra.write_text("synthetic")
    unlink = Path.unlink
    def fail_extra(path, *args, **kwargs):
        if path == extra:
            raise PermissionError("synthetic")
        return unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_extra)
    result = sg.cleanup_duplicate_strms()
    assert result["cleaned"] == 0 and result["errors"] == 1


def test_requeue_start_failure_is_not_counted_as_submitted(monkeypatch, tmp_path):
    from unittest.mock import Mock
    import strm_generator as sg
    monkeypatch.setattr(sg, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(sg, "_maintenance_lock", threading.Lock())
    monkeypatch.setattr(sg, "db", SimpleNamespace(get_virtual_items_by_imdb=lambda *args: []))
    monkeypatch.setitem(sys.modules, "processor", SimpleNamespace(process=Mock()))
    monkeypatch.setattr(threading, "Thread", Mock(return_value=SimpleNamespace(
        start=Mock(side_effect=RuntimeError("synthetic")))))
    folder = tmp_path / "movies" / "Example"
    folder.mkdir(parents=True)
    (folder / "Example.nfo").write_text("<imdbid>tt1234567</imdbid>")
    result = sg.repair_expired_strms()
    assert result["requeued"] == 0 and result["errors"] == 1
