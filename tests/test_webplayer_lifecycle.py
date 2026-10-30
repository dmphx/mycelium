"""Clock-controlled playback lifetime and owned generation retirement."""
from types import SimpleNamespace

from flask import Flask
import pytest

from plugins.webplayer import routes
from plugins.webplayer import web_player as wp
from test_webplayer_generations import playback  # Synthetic process/file fixture.


@pytest.fixture()
def owned(playback, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(wp, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    direct = playback()
    direct.last_request = now[0]
    wp._do_hls_conversion(direct.token, direct.file_info)
    current = wp.get_session(direct.token, owner_id=7)
    current.last_request = now[0]
    (current.tmp_dir / 'sub_0_eng.vtt').write_text('WEBVTT\n')
    app = Flask(__name__)
    app.register_blueprint(routes.bp)
    app.config['TESTING'] = True
    actor = [7]
    monkeypatch.setattr(routes, '_check_enabled', lambda: {'id': actor[0]})
    monkeypatch.setattr(routes.positions, 'save', lambda *args: None)
    return direct, current, app.test_client(), now, actor


@pytest.mark.parametrize('activity', ['segment', 'subtitle', 'position', 'status'])
def test_successful_owned_activity_survives_cleanup_beyond_original_lifetime(owned, activity):
    direct, current, client, now, _ = owned
    now[0] = 2000.0
    base = '/stream/' + direct.token
    if activity == 'position':
        response = client.post(base + '/position', json={'position_s': 10, 'generation': current.generation})
    else:
        suffix = {'segment': '/hls/' + current.generation + '/seg00000.ts',
                  'subtitle': '/subtitles/' + current.generation + '/sub_0_eng.vtt',
                  'status': '/hls-status'}[activity]
        response = client.get(base + suffix)
    assert response.status_code == 200
    response.close()
    now[0] = 2901.0  # Initial direct-session timestamp is now older than 1800s.
    wp.cleanup_idle_sessions()
    assert wp.get_direct_session(direct.token, owner_id=7) is direct
    assert wp.get_session(direct.token, owner_id=7) is current
    assert current.tmp_dir.exists() and current.proc.poll() is None


def test_direct_position_activity_also_keeps_owner_session_alive(owned):
    direct, current, client, now, _ = owned
    wp._sessions.clear()
    wp._generations.clear()
    wp._stop_process(current.proc)
    now[0] = 2000.0
    assert client.post('/stream/' + direct.token + '/position', json={'position_s': 10}).status_code == 200
    now[0] = 2901.0
    wp.cleanup_idle_sessions()
    assert wp.get_direct_session(direct.token, owner_id=7) is direct


def test_idle_token_retires_all_generations_and_unpublished_attempt_directories(owned):
    direct, first, _, now, _ = owned
    generations = [first]
    for position in (10, 20):
        assert wp.seek_session(direct.token, position, owner_id=7)
        generations.append(wp.get_session(direct.token, owner_id=7))
    unpublished = wp._generation_dir(direct)
    (unpublished / 'failed-output.ts').write_bytes(b'failed synthetic attempt')
    for session in generations:
        session.last_request = 1000.0
    now[0] = 2801.0
    wp.cleanup_idle_sessions()
    assert wp.get_direct_session(direct.token, owner_id=7) is None
    assert wp.get_session(direct.token, owner_id=7) is None
    assert not any(key[0] == direct.token for key in wp._generations)
    assert not (wp.PLAYER_TMP_DIR / direct.token).exists()
    assert all(s.proc.poll() is not None and s.proc.waits >= 1 for s in generations)


def test_recent_old_generation_reader_preserves_the_whole_owned_token(owned):
    direct, first, client, now, _ = owned
    assert wp.seek_session(direct.token, 20, owner_id=7)
    current = wp.get_session(direct.token, owner_id=7)
    current.last_request = 1000.0
    now[0] = 2000.0
    response = client.get('/stream/' + direct.token + '/hls/' + first.generation + '/seg00000.ts')
    assert response.status_code == 200
    response.close()
    now[0] = 2901.0
    wp.cleanup_idle_sessions()
    assert wp.get_session(direct.token, owner_id=7) is current
    assert wp.get_generation(direct.token, first.generation, owner_id=7) is first
    assert first.tmp_dir.exists() and current.tmp_dir.exists()


def test_cleanup_skips_an_owned_conversion_or_seek_in_progress(owned):
    direct, current, _, now, _ = owned
    now[0] = 2801.0
    direct.operation_lock.acquire()
    try:
        wp.cleanup_idle_sessions()
        assert wp.get_session(direct.token, owner_id=7) is current
        assert current.proc.poll() is None and current.tmp_dir.exists()
    finally:
        direct.operation_lock.release()
    wp.cleanup_idle_sessions()
    assert wp.get_session(direct.token, owner_id=7) is None


def test_foreign_missing_and_invalid_activity_do_not_renew_token(owned):
    direct, current, client, now, actor = owned
    now[0] = 2000.0
    path = '/stream/' + direct.token
    actor[0] = 8
    assert client.get(path + '/hls/' + current.generation + '/seg00000.ts').status_code == 404
    actor[0] = 7
    assert client.get(path + '/hls/' + current.generation + '/seg99999.ts').status_code == 404
    assert client.post(path + '/position', json={'position_s': -1, 'generation': current.generation}).status_code == 400
    assert direct.last_request == current.last_request == 1000.0
    now[0] = 2801.0
    wp.cleanup_idle_sessions()
    assert wp.get_direct_session(direct.token, owner_id=7) is None


def test_failed_retirement_stays_unavailable_and_retries_cleanup_only(owned, monkeypatch):
    direct, current, client, now, _ = owned
    now[0] = 2801.0
    original = wp._stop_process
    monkeypatch.setattr(wp, '_stop_process', lambda proc: (_ for _ in ()).throw(TimeoutError()))
    wp.cleanup_idle_sessions()
    assert wp.get_direct_session(direct.token, owner_id=7) is None
    assert direct.retiring and direct.token in wp._direct_sessions
    assert client.get('/stream/' + direct.token + '/hls/' + current.generation + '/seg00000.ts').status_code == 404
    assert current.tmp_dir.exists() and not direct.operation_lock.locked()
    assert wp.start_hls_conversion(direct.token, owner_id=7) is False
    monkeypatch.setattr(wp, '_stop_process', original)
    wp.cleanup_idle_sessions()
    assert direct.token not in wp._direct_sessions
    assert not wp._generations and not current.tmp_dir.exists()


def test_activity_between_candidate_snapshot_and_lock_is_rechecked(owned):
    direct, current, _, now, _ = owned
    now[0] = 2801.0
    real_lock = direct.operation_lock
    class RenewBeforeReserve:
        def acquire(self, **kwargs):
            assert wp.touch_playback(direct.token, owner_id=7, generation=current.generation)
            return real_lock.acquire(**kwargs)
        def release(self):
            real_lock.release()
    direct.operation_lock = RenewBeforeReserve()
    wp.cleanup_idle_sessions()
    assert wp.get_direct_session(direct.token, owner_id=7) is direct
    assert current.tmp_dir.exists() and current.proc.poll() is None


def test_disk_cleanup_failure_keeps_retirement_for_next_sweep(owned, monkeypatch):
    direct, current, _, now, _ = owned
    now[0] = 2801.0
    original = wp.shutil.rmtree
    def disk_failure(path):
        raise OSError('synthetic disk fault')
    monkeypatch.setattr(wp.shutil, 'rmtree', disk_failure)
    wp.cleanup_idle_sessions()
    assert direct.retiring and wp.get_direct_session(direct.token, owner_id=7) is None
    assert wp._generations[(direct.token, current.generation)] is current
    assert current.tmp_dir.exists() and current.proc.poll() is not None
    monkeypatch.setattr(wp.shutil, 'rmtree', original)
    wp.cleanup_idle_sessions()
    assert not wp._generations and direct.token not in wp._direct_sessions


def test_retirement_of_one_token_does_not_remove_recent_other_owner(owned, playback):
    direct, _, _, now, _ = owned
    other = playback(token='b' * 32, owner=8)
    other.last_request = 2000.0
    wp._do_hls_conversion(other.token, other.file_info)
    current = wp.get_session(other.token, owner_id=8)
    current.last_request = 2000.0
    now[0] = 2801.0
    wp.cleanup_idle_sessions()
    assert direct.token not in wp._direct_sessions
    assert wp.get_direct_session(other.token, owner_id=8) is other
    assert wp.get_generation(other.token, current.generation, owner_id=8) is current
    assert current.tmp_dir.exists() and current.proc.poll() is None
