"""Conversation API over the existing local server. The agent is a script, never a model."""
from contextlib import closing, contextmanager
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading

from types import SimpleNamespace

import httpx
import pytest

from sql_agent_lab.macsql import live_ui


@contextmanager
def client_for(app):
    server = ThreadingHTTPServer(('127.0.0.1', 0), live_ui.make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def database(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE numbers(n INTEGER)')
        conn.execute('INSERT INTO numbers VALUES (?)', (value,))
        conn.commit()


def build(tmp_path, monkeypatch, *, workspace=True):
    root = tmp_path / 'SECRET_DB_PATH'
    database(root / 'business.sqlite', 42)
    database(root / 'archive.sqlite', 7)
    config = tmp_path / 'sources.json'
    config.write_text(json.dumps({'sources': [
        {'id': 'shop', 'name': 'Shop', 'path': 'SECRET_DB_PATH/business.sqlite'},
        {'id': 'archive', 'name': 'Archive', 'path': 'SECRET_DB_PATH/archive.sqlite'},
    ]}), encoding='utf-8')
    before = {path: path.read_bytes() for path in root.glob('*.sqlite')}
    app = live_ui.Observatory(
        None, tmp_path / 'runs', sources=config,
        workspace_db=(tmp_path / 'workspace.sqlite') if workspace else None)
    monkeypatch.setattr(live_ui, 'LocalOllamaModel', lambda *a, **kw: SimpleNamespace(metadata={}))

    class FakeAgent:
        def __init__(self, models, *, config):
            self.config = config

        def run(self, question):
            return app.behavior(self.config, question)

    monkeypatch.setattr(live_ui, 'ReferenceMacSQLAgent', FakeAgent)
    app.behavior = lambda config, question: 'SELECT n FROM numbers /* gold_sentinel */'
    app.worker_done = threading.Event()
    original = app.work

    def work(*args):
        try:
            original(*args)
        finally:
            app.worker_done.set()

    monkeypatch.setattr(app, 'work', work)
    app.business_before = before
    return app


def wait_done(app):
    assert app.worker_done.wait(5)
    assert app.active is None


def test_workspace_off_leaves_existing_api_unchanged(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, workspace=False)
    with client_for(app) as client:
        assert client.get('/api/conversations').status_code == 404
        assert client.post('/api/conversations', json={'datasource_id': 'shop'}).status_code == 404
        catalog = client.get('/api/catalog').json()
        assert catalog['databases'] == ['archive', 'shop']
        assert all('path' not in source for source in catalog['sources'])
        app.worker_done.clear()
        response = client.post('/api/start', json={'db_id': 'shop', 'question': 'How many rows?'})
        assert response.status_code == 200
        wait_done(app)
        assert client.get('/api/run', params={'id': response.json()['id']}).json()['task']['state'] == 'completed'
    assert list(tmp_path.rglob('workspace.sqlite')) == []
    assert all(path.read_bytes() == content for path, content in app.business_before.items())


def test_conversation_messages_survive_restart_and_keep_their_source(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    with client_for(app) as client:
        assert client.post('/api/conversations', json={'datasource_id': 'missing'}).status_code == 400
        assert client.get('/api/conversations/not-a-conversation').status_code == 400
        created = client.post('/api/conversations', json={'datasource_id': 'shop', 'title': 'Orders'}).json()
        assert created['datasource_id'] == 'shop'
        assert created['title'] == 'Orders'
        listed = client.get('/api/conversations').json()['conversations']
        assert [item['id'] for item in listed] == [created['id']]
        assert client.post(
            f"/api/conversations/{created['id']}/messages",
            json={'question': 'How many rows?', 'datasource_id': 'archive'}).status_code == 400
        assert client.post(
            f"/api/conversations/{created['id']}/messages",
            json={'question': 'How many rows?', 'db_id': 'archive'}).status_code == 400

        app.worker_done.clear()
        first = client.post(
            f"/api/conversations/{created['id']}/messages",
            json={'question': 'How many rows?'}).json()
        wait_done(app)
        app.worker_done.clear()
        second = client.post(
            f"/api/conversations/{created['id']}/messages",
            json={'question': 'How many are left?'}).json()
        wait_done(app)

    restarted = live_ui.Observatory(None, app.output, sources=tmp_path / 'sources.json',
                                    workspace_db=app.workspace_db)
    with client_for(restarted) as client:
        detail = client.get(f"/api/conversations/{created['id']}").json()
        body = json.dumps(detail)
        assert [item['seq'] for item in detail['messages']] == [1, 2]
        assert [item['body'] for item in detail['messages']] == ['How many rows?', 'How many are left?']
        assert [item['id'] for item in detail['runs']] == [first['id'], second['id']]
        assert {item['conversation_id'] for item in (first, second)} == {created['id']}
        assert {item['user_message_id'] for item in detail['runs']} == {item['id'] for item in detail['messages']}
        assert all(item['datasource_id'] == 'shop' for item in detail['runs'])
        assert all(item['state'] == 'completed' for item in detail['runs'])
        assert detail['conversation']['datasource_id'] == 'shop'
        assert all('gold_sentinel' not in item['body'] for item in detail['messages'])
        assert 'SECRET_DB_PATH' not in body
        assert 'business.sqlite' not in body
        assert 'artifact_dir' not in body
        other = client.post('/api/conversations', json={'datasource_id': 'archive'}).json()
        other_detail = client.get(f"/api/conversations/{other['id']}").json()
        assert other_detail['messages'] == []
        assert other_detail['conversation']['datasource_id'] == 'archive'
        assert len(client.get('/api/conversations').json()['conversations']) == 2
    assert all(path.read_bytes() == content for path, content in app.business_before.items())


def test_stop_failure_and_workspace_write_error(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def blocked(config, question):
        entered.set()
        assert release.wait(5)
        return 'SELECT n FROM numbers'

    app.behavior = blocked
    with client_for(app) as client:
        created = client.post('/api/conversations', json={'datasource_id': 'shop'}).json()
        app.worker_done.clear()
        started = client.post(f"/api/conversations/{created['id']}/messages",
                              json={'question': 'Stop this question'})
        assert started.status_code == 200
        assert entered.wait(5)
        queued = client.post(f"/api/conversations/{created['id']}/messages",
                             json={'question': 'Second while active'})
        assert queued.status_code == 200
        assert client.post('/api/stop', json={'id': queued.json()['id']}).json()['status'] == 'cancelled'
        assert client.post('/api/stop', json={'id': started.json()['id']}).json()['status'] == 'stop_requested'
        release.set()
        wait_done(app)
        detail = client.get(f"/api/conversations/{created['id']}").json()
        assert detail['runs'][0]['state'] == 'stopped'
        assert [item['body'] for item in detail['messages']] == ['Stop this question', 'Second while active']

        def fail(config, question):
            raise RuntimeError('offline injected failure')

        app.behavior = fail
        app.worker_done.clear()
        failed = client.post(f"/api/conversations/{created['id']}/messages",
                             json={'question': 'This question fails'})
        assert failed.status_code == 200
        wait_done(app)
        detail = client.get(f"/api/conversations/{created['id']}").json()
        assert detail['runs'][-1]['state'] == 'failed'
        assert detail['runs'][-1]['error'] == 'offline injected failure'
        assert [item['body'] for item in detail['messages']] == [
            'Stop this question', 'Second while active', 'This question fails']

        import sql_agent_lab.workspace.database as workspace_db

        def unwritable(self, **kwargs):
            raise sqlite3.OperationalError('workspace write failed')

        monkeypatch.setattr(workspace_db.WorkspaceStore, 'attach_run', unwritable)
        def unexpected_worker(*args):
            pytest.fail('An unrecorded conversation message must never launch a worker')
        monkeypatch.setattr(app, 'work', unexpected_worker)
        app.worker_done.clear()
        rejected = client.post(f"/api/conversations/{created['id']}/messages",
                               json={'question': 'This write should fail'})
        assert rejected.status_code == 500
        assert rejected.json()['error'] == 'workspace write failed'
        assert app.active is None
        assert not app.worker_done.is_set()
        detail = client.get(f"/api/conversations/{created['id']}").json()
        assert [item['body'] for item in detail['messages']] == [
            'Stop this question', 'Second while active', 'This question fails']
    assert all(path.read_bytes() == content for path, content in app.business_before.items())
