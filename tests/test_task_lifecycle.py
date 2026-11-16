"""Exercise the real HTTP/worker/storage boundary with a fake Agent, never inference."""
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from sql_agent_lab.macsql import live_ui
from sql_agent_lab.macsql.fetch_bird import sha256
from sql_agent_lab.macsql.task_lifecycle import change_task, new_task, write_json_atomic


@pytest.fixture
def app(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    db = data / 'demo.sqlite'
    with sqlite3.connect(db) as conn:
        conn.executescript('CREATE TABLE numbers(n INTEGER); INSERT INTO numbers VALUES (42);')
    questions = data / 'mini_dev_sqlite.jsonl'
    questions.write_text(json.dumps({'question_id': 1, 'db_id': 'demo',
                                    'question': 'Return n', 'SQL': 'SELECT n FROM numbers'}) + '\n')
    (data / 'source-lock.json').write_text(json.dumps({
        'annotations_sha256': sha256(questions),
        'databases': {'demo': {'path': 'demo.sqlite', 'sha256': sha256(db)}}}))
    instance = live_ui.Observatory(data, tmp_path / 'runs')
    # Construction and inference are both replaced; no Ollama request can occur.
    monkeypatch.setattr(live_ui, 'LocalOllamaModel', lambda *a, **kw: SimpleNamespace(metadata={}))

    class FakeAgent:
        def __init__(self, models, *, config):
            self.config = config

        def run(self, question):
            return instance.behavior(self.config, question)

    monkeypatch.setattr(live_ui, 'ReferenceMacSQLAgent', FakeAgent)
    instance.behavior = lambda config, question: 'SELECT n FROM numbers'
    original_work = instance.work
    instance.worker_done = threading.Event()

    def work(*args):
        try:
            original_work(*args)
        finally:
            instance.worker_done.set()

    monkeypatch.setattr(instance, 'work', work)
    return instance


def wait_done(app):
    assert app.worker_done.wait(5), 'Offline worker did not finish'
    assert app.active is None


@pytest.mark.parametrize('sql,score', [('SELECT n FROM numbers', 1), ('SELECT n+1 FROM numbers', 0)])
def test_completed_task_is_independent_of_answer_score(app, sql, score):
    app.behavior = lambda config, question: sql
    run_id = app.start({'question_id': '1'})
    wait_done(app)
    run = app.read_run(run_id)
    assert run['task']['state'] == 'completed'
    assert run['task']['started_at'] <= run['task']['ended_at']
    assert run['result']['score'] == score
    assert run['result']['semantic_status'] == 'not_completed'  # Fake Agent emits no workflow completion.
    assert app.run_index()['runs'][0]['task'] == run['task']
    restarted = live_ui.Observatory(app.data, app.output)
    assert restarted.read_run(run_id)['task'] == run['task']


def test_type_review_does_not_become_task_failure(app):
    def behavior(config, question):
        config.trace({'event': 'workflow_completed', 'output_type_check': {'status': 'mismatch'}})
        return 'SELECT n FROM numbers'
    app.behavior = behavior
    run_id = app.start({'question_id': '1'})
    wait_done(app)
    run = app.read_run(run_id)
    assert run['result']['status'] == 'needs_review'
    assert run['result']['score'] == 1
    assert run['task']['state'] == 'completed'


def test_worker_failure_is_persisted_and_releases_slot(app):
    def fail(config, question):
        raise RuntimeError('offline injected failure')
    app.behavior = fail
    run_id = app.start({'question_id': '1'})
    wait_done(app)
    task = app.read_run(run_id)['task']
    assert task['state'] == 'failed'
    assert task['error'] == 'offline injected failure'
    assert task['ended_at']


def test_http_stop_is_targeted_idempotent_and_waits_for_worker(app):
    entered, release = threading.Event(), threading.Event()

    def blocked(config, question):
        config.trace({'event': 'call_started', 'role': 'decomposer'})
        entered.set()
        assert release.wait(5)
        return 'SELECT n FROM numbers'

    app.behavior = blocked
    server = ThreadingHTTPServer(('127.0.0.1', 0), live_ui.make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            run_id = client.post('/api/start', json={'question_id': '1'}).json()['id']
            assert entered.wait(5)
            detail = client.get('/api/run', params={'id': run_id}).json()
            assert detail['task']['state'] == 'running'
            assert detail['task']['stage'] == 'decomposer'
            assert client.post('/api/start', json={'question_id': '1'}).status_code == 400
            assert client.post('/api/stop', json={}).status_code == 400
            assert client.post('/api/stop', json={'id': 'old-run'}).json()['status'] == 'not_active'
            assert not app.stop.is_set()
            assert client.post('/api/stop', json={'id': run_id}).json()['status'] == 'stop_requested'
            stopped_at = app.read_run(run_id)['task']['stop_requested_at']
            client.post('/api/stop', json={'id': run_id})
            detail = client.get('/api/run', params={'id': run_id}).json()
            assert detail['active'] and detail['stop_requested']
            assert detail['task']['state'] == 'stopping'
            assert detail['task']['stop_requested_at'] == stopped_at
            assert detail['task']['ended_at'] is None
            release.set()
            wait_done(app)
            detail = client.get('/api/run', params={'id': run_id}).json()
            assert detail['task']['state'] == 'stopped'
            assert detail['result']['score'] is None
            assert client.post('/api/stop', json={'id': run_id}).json()['status'] == 'not_active'
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join()


def test_restart_projects_interruption_without_rewriting_history(app, tmp_path):
    path = app.output / 'abandoned'
    path.mkdir()
    write_json_atomic(path / 'manifest.json', {'question': {'question_id': '1'}})
    write_json_atomic(path / 'task.json', change_task(new_task(), state='stopping'))
    (path / 'trace.jsonl').write_text('{"event":"workflow_started"}\n{"partial":')
    external = tmp_path / 'shared-history'
    external.mkdir()
    old = external / 'local-pilot-old'
    old.mkdir()
    write_json_atomic(old / 'manifest.json', {'question': {'question_id': 'old'}})
    before = {p: p.read_bytes() for root in (path, external) for p in root.rglob('*') if p.is_file()}
    restarted = live_ui.Observatory(app.data, app.output, history=external)
    detail = restarted.read_run('abandoned')
    assert detail['task']['state'] == 'interrupted'
    assert detail['task']['ended_at'] is None  # Exact crash time is unknown.
    assert detail['result'] is None and not detail['active']
    assert len(detail['events']) == 1
    assert restarted.read_run('history-local-pilot-old')['task']['state'] == 'unknown'
    restarted.run_index()
    assert all(p.read_bytes() == value for p, value in before.items())
    assert not (old / 'task.json').exists()
    # Result saved just before a crash must win over an unfinished task snapshot.
    write_json_atomic(path / 'result.json', {'status': 'completed', 'score': 0})
    detail = restarted.read_run('abandoned')
    assert detail['task']['state'] == 'completed'
    assert detail['task']['source'] == 'result_recovery'
    assert detail['result']['score'] == 0


def test_result_write_failure_still_releases_slot_and_reports_failure(app, monkeypatch):
    original = live_ui.write_json_atomic
    def write(path, value):
        if path.name == 'result.json':
            raise OSError('offline disk write failure')
        original(path, value)
    monkeypatch.setattr(live_ui, 'write_json_atomic', write)
    run_id = app.start({'question_id': '1'})
    wait_done(app)
    detail = app.read_run(run_id)
    assert detail['task']['state'] == 'failed'
    assert detail['task']['error'] == 'offline disk write failure'
    assert detail['result'] is None


def test_task_write_failure_remains_visible_in_memory(app, monkeypatch):
    original = live_ui.write_json_atomic
    def write(path, value):
        if path.name == 'task.json' and value['state'] == 'completed':
            raise OSError('offline task write failure')
        original(path, value)
    monkeypatch.setattr(live_ui, 'write_json_atomic', write)
    run_id = app.start({'question_id': '1'})
    wait_done(app)
    detail = app.read_run(run_id)
    assert detail['task']['state'] == 'completed'
    assert detail['task']['persistence_error'] == 'offline task write failure'


def test_successful_save_clears_previous_persistence_warning(app, monkeypatch):
    path = app.output / 'save-recovery'
    path.mkdir()
    original = live_ui.write_json_atomic
    attempts = 0

    def fail_once(path, value):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError('temporary disk failure')
        original(path, value)

    monkeypatch.setattr(live_ui, 'write_json_atomic', fail_once)
    with app.lock:
        with pytest.raises(OSError, match='temporary disk failure'):
            app._save_task(path.name, new_task())
        assert app.tasks[path.name]['persistence_error'] == 'temporary disk failure'
        app._save_task(path.name, change_task(app.tasks[path.name], state='failed', error='run failed'))
    assert 'persistence_error' not in app.tasks[path.name]
    saved = json.loads((path / 'task.json').read_text(encoding='utf8'))
    assert 'persistence_error' not in saved
    assert saved['error'] == 'run failed'  # Only the obsolete storage warning disappears.


def test_thread_start_failure_does_not_leave_active_task(app, monkeypatch):
    def fail(*args):
        raise RuntimeError('offline thread start failure')
    monkeypatch.setattr(threading.Thread, 'start', fail)
    with pytest.raises(RuntimeError, match='thread start failure'):
        app.start({'question_id': '1'})
    assert app.active is None
    assert app.run_index()['runs'][0]['task']['state'] == 'failed'


def test_finished_task_cannot_return_to_running():
    task = change_task(new_task(), state='completed')
    with pytest.raises(ValueError, match='Invalid task transition'):
        change_task(task, state='running')
