"""Actual tree/status functions, with in-memory DB and provider boundaries."""
import ast
import logging
from pathlib import Path
import threading
from types import SimpleNamespace

from flask import Flask
import pytest


@pytest.fixture()
def tree_app():
    # Compile the actual finite functions instead of booting provider/scheduler
    # startup. No app behavior is reimplemented and no network is contacted.
    source = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text())
    names = {'_refresh_spore_status', '_spore_status_for', '_build_spore_tree_json', 'spore_nfs_tree'}
    functions = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in functions} == names
    app = Flask(__name__)
    item = {'token': 'a' * 16, 'info_hash': 'b' * 40, 'strm_path': '/synthetic/movies/Film/Film.strm'}
    torrents = [dict(hash=item['info_hash'], download_state='completed', measured_size=1326830205)]
    calls = []
    def provider(**kwargs):
        calls.append(kwargs)
        return iter(torrents)
    ns = {'app': app, 'log': logging.getLogger('synthetic-spore-tree'),
          'threading': threading, '_spore_status': {}, '_spore_status_lock': threading.Lock(),
          '_spore_status_ready': threading.Event(), '_SPORE_READY_STATES': {'cached', 'completed'},
          '_spore_tree_cache': {'body': None, 'ts': 0.0}, '_spore_tree_lock': threading.Lock(),
          '_SPORE_TREE_TTL': 30.0, '_os': SimpleNamespace(environ={}),
          'db': SimpleNamespace(get_virtual_item_spore_index=lambda: [item], get_all_virtual_items=lambda: [item]),
          'torbox': SimpleNamespace(iter_torrents=provider),
          'strm_generator': SimpleNamespace(spore_item_size=lambda torrent, item: torrent['measured_size'])}
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'app.py:tree-startup-functions', 'exec'), ns)
    return SimpleNamespace(ns=ns, client=app.test_client(), calls=calls, item=item, torrents=torrents)


def test_first_tree_waits_for_complete_initial_status_sweep(tree_app):
    f = tree_app
    for _ in range(2):
        response = f.client.get('/spore-nfs/tree')
        assert response.status_code == 503
        assert response.headers['Retry-After'] == '10'
        assert response.headers['Cache-Control'] == 'no-store'
        assert f.ns['_spore_tree_cache']['body'] is None
    assert not f.calls  # Tree retries cannot trigger materialization/provider work.
    f.ns['_refresh_spore_status']()
    response = f.client.get('/spore-nfs/tree')
    assert response.status_code == 200
    assert response.json['entries'] == [dict(token=f.item['token'], path='movies/Film/Film.mkv', cached=True, size=1326830205)]
    assert len(f.calls) == 1


def test_partial_sweep_cannot_publish_a_placeholder_tree(tree_app):
    f = tree_app
    entered, release = threading.Event(), threading.Event()
    def paused(**kwargs):
        yield f.torrents[0]
        entered.set()
        assert release.wait(5)
    f.ns['torbox'].iter_torrents = paused
    worker = threading.Thread(target=f.ns['_refresh_spore_status'])
    worker.start()
    try:
        assert entered.wait(5)
        assert f.client.get('/spore-nfs/tree').status_code == 503
        assert not f.ns['_spore_status']
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert f.client.get('/spore-nfs/tree').json['entries'][0]['size'] == 1326830205


@pytest.mark.parametrize('failure', ['db', 'provider'])
def test_failed_initial_sweep_keeps_tree_unavailable_until_success(tree_app, failure):
    f = tree_app
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic initial status failure')
    target, name = (f.ns['db'], 'get_virtual_item_spore_index') if failure == 'db' else (f.ns['torbox'], 'iter_torrents')
    original = getattr(target, name)
    setattr(target, name, fail)
    f.ns['_refresh_spore_status']()
    assert f.client.get('/spore-nfs/tree').status_code == 503
    assert not f.ns['_spore_status_ready'].is_set()
    setattr(target, name, original)
    f.ns['_refresh_spore_status']()
    assert f.client.get('/spore-nfs/tree').json['entries'][0]['cached'] is True


def test_failed_later_sweep_retains_last_good_published_state(tree_app):
    f = tree_app
    f.ns['_refresh_spore_status']()
    first = f.client.get('/spore-nfs/tree')
    assert first.status_code == 200
    def fail(**kwargs):
        raise RuntimeError('synthetic later provider failure')
    f.ns['torbox'].iter_torrents = fail
    f.ns['_refresh_spore_status']()
    assert f.client.get('/spore-nfs/tree').json == first.json
    assert f.ns['_spore_status_ready'].is_set()


def test_complete_empty_sweep_is_ready_without_inventing_cached_media(tree_app):
    f = tree_app
    f.ns['db'].get_virtual_item_spore_index = lambda: []
    f.ns['db'].get_all_virtual_items = lambda: []
    f.ns['torbox'].iter_torrents = lambda **kwargs: iter(())
    f.ns['_refresh_spore_status']()
    response = f.client.get('/spore-nfs/tree')
    assert response.status_code == 200 and response.json == {'entries': []}
