"""Workspace records follow a saved run directory and do not replace it."""
from contextlib import closing
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from sql_agent_lab.macsql import live_ui
from sql_agent_lab.macsql.fetch_bird import sha256
from sql_agent_lab.workspace.database import (
    LOCAL_USER_ID, WorkspaceStore, record_saved_run,
)


def build_app(tmp_path, monkeypatch, *, workspace_db=None, sql='SELECT gold_sentinel FROM numbers',
              evidence='schema-sample-sentinel'):
    data = tmp_path / 'data'
    data.mkdir()
    db = data / 'demo.sqlite'
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript('CREATE TABLE numbers(n INTEGER); INSERT INTO numbers VALUES (42);')
    questions = data / 'mini_dev_sqlite.jsonl'
    questions.write_text(json.dumps({
        'question_id': 1, 'db_id': 'demo', 'question': 'Return n',
        'evidence': evidence, 'SQL': sql}) + '\n', encoding='utf-8')
    (data / 'source-lock.json').write_text(json.dumps({
        'annotations_sha256': sha256(questions),
        'databases': {'demo': {'path': 'demo.sqlite', 'sha256': sha256(db)}}}))
    before = db.read_bytes()
    app = live_ui.Observatory(data, tmp_path / 'runs', workspace_db=workspace_db)
    monkeypatch.setattr(live_ui, 'LocalOllamaModel', lambda *a, **kw: SimpleNamespace(metadata={}))

    class FakeAgent:
        def __init__(self, models, *, config):
            self.config = config

        def run(self, question):
            return app.behavior(self.config, question)

    monkeypatch.setattr(live_ui, 'ReferenceMacSQLAgent', FakeAgent)
    app.behavior = lambda config, question: 'SELECT n FROM numbers'
    app.worker_done = threading.Event()
    original = app.work

    def work(*args):
        try:
            original(*args)
        finally:
            app.worker_done.set()

    monkeypatch.setattr(app, 'work', work)
    app.business_bytes = before
    app.business_path = db
    return app


def wait_done(app):
    assert app.worker_done.wait(5)
    assert app.active is None


def saved_task(app, run_id):
    return json.loads((app.output / run_id / 'task.json').read_text(encoding='utf8'))


def test_closed_flag_does_not_record_a_conversation(tmp_path, monkeypatch):
    app = build_app(tmp_path, monkeypatch)
    run_id = app.start({'db_id': 'demo', 'question': 'How many rows?'})
    wait_done(app)
    task = saved_task(app, run_id)
    assert task['state'] == 'completed'
    assert 'persistence_error' not in task
    assert app.read_run(run_id)['result']['score'] is None
    assert list(tmp_path.rglob('workspace.sqlite')) == []
    assert app.business_path.read_bytes() == app.business_bytes


def test_custom_query_links_conversation_message_and_run(tmp_path, monkeypatch):
    database = tmp_path / 'workspace.sqlite'
    app = build_app(tmp_path, monkeypatch, workspace_db=database)
    run_id = app.start({'db_id': 'demo', 'question': 'How many rows?'})
    wait_done(app)
    store = WorkspaceStore(database)
    run = store.get_agent_run(run_id)
    conversation = store.get_conversation(run.conversation_id)
    messages = store.list_messages(conversation.id)
    assert run.datasource_id == 'demo'
    assert run.state == 'completed'
    assert run.result_status == 'completed'
    assert run.score is None
    assert run.user_message_id == messages[0].id
    assert conversation.datasource_id == 'demo'
    assert conversation.user_id == LOCAL_USER_ID
    assert len(messages) == 1
    assert messages[0].role == 'user'
    assert messages[0].body == 'How many rows?'
    assert 'gold_sentinel' not in messages[0].to_json()
    assert 'schema-sample-sentinel' not in messages[0].to_json()
    assert 'artifact_dir' not in run.to_json()
    assert len(store.list_agent_runs()) == 1

    path = app.output / run_id
    record_saved_run(
        store, run_id=run_id,
        manifest=json.loads((path / 'manifest.json').read_text(encoding='utf8')),
        task=saved_task(app, run_id),
        result=json.loads((path / 'result.json').read_text(encoding='utf8')),
        artifact_dir=str(path.resolve()))
    assert len(store.list_agent_runs()) == 1
    assert len(store.list_messages(conversation.id)) == 1

    restarted = live_ui.Observatory(app.data, app.output, workspace_db=database)
    assert restarted.read_run(run_id)['task']['state'] == 'completed'
    assert WorkspaceStore(database).get_agent_run(run_id) == run
    assert app.business_path.read_bytes() == app.business_bytes

    app.worker_done.clear()
    second = app.start({'db_id': 'demo', 'question': 'How many rows again?'})
    wait_done(app)
    runs = WorkspaceStore(database).list_agent_runs()
    assert {item.id for item in runs} == {run_id, second}
    assert len({item.conversation_id for item in runs}) == 2


def test_bird_reference_sql_stays_out_of_the_user_message(tmp_path, monkeypatch):
    database = tmp_path / 'workspace.sqlite'
    app = build_app(tmp_path, monkeypatch, workspace_db=database,
                    sql='SELECT n FROM numbers /* gold_sentinel */')
    run_id = app.start({'question_id': '1'})
    wait_done(app)
    store = WorkspaceStore(database)
    run = store.get_agent_run(run_id)
    message = store.list_messages(run.conversation_id)[0]
    assert message.body == 'Return n'
    assert run.datasource_id == 'demo'
    assert run.score == 1
    blob = database.read_bytes()
    assert b'gold_sentinel' not in blob
    assert b'schema-sample-sentinel' not in blob
    assert b'demo.sqlite' not in blob
    text = message.to_json() + store.get_conversation(run.conversation_id).to_json() + run.to_json()
    assert 'gold_sentinel' not in text
    assert 'schema-sample-sentinel' not in text


def test_failure_stop_and_interrupt_keep_one_run_record(tmp_path, monkeypatch):
    database = tmp_path / 'workspace.sqlite'
    app = build_app(tmp_path, monkeypatch, workspace_db=database)

    def fail(config, question):
        raise RuntimeError('offline injected failure')

    app.behavior = fail
    failed_id = app.start({'db_id': 'demo', 'question': 'How many rows?'})
    wait_done(app)
    failed = WorkspaceStore(database).get_agent_run(failed_id)
    assert failed.state == 'failed'
    assert failed.error == 'offline injected failure'
    assert WorkspaceStore(database).list_messages(failed.conversation_id)[0].body == 'How many rows?'

    entered, release = threading.Event(), threading.Event()

    def blocked(config, question):
        entered.set()
        assert release.wait(5)
        return 'SELECT n FROM numbers'

    app.behavior = blocked
    app.worker_done.clear()
    stopped_id = app.start({'db_id': 'demo', 'question': 'Stop this question'})
    assert entered.wait(5)
    assert app.request_stop(stopped_id)['status'] == 'stop_requested'
    release.set()
    wait_done(app)
    assert WorkspaceStore(database).get_agent_run(stopped_id).state == 'stopped'

    app.worker_done.clear()
    entered.clear()
    release.clear()
    abandoned_id = app.start({'db_id': 'demo', 'question': 'Abandon this question'})
    assert entered.wait(5)
    try:
        folder = app.output / abandoned_id
        before = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
        restarted = live_ui.Observatory(app.data, app.output, workspace_db=database)
        assert restarted.read_run(abandoned_id)['task']['state'] == 'interrupted'
        assert {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()} == before
        assert WorkspaceStore(database).get_agent_run(abandoned_id).state == 'running'
        assert len(WorkspaceStore(database).list_agent_runs()) == 3
    finally:
        release.set()
        wait_done(app)


def test_workspace_write_failure_is_not_reported_as_saved(tmp_path, monkeypatch):
    database = tmp_path / 'workspace.sqlite'
    app = build_app(tmp_path, monkeypatch, workspace_db=database)
    import sql_agent_lab.workspace.database as workspace_db

    def fail(self, run):
        raise sqlite3.OperationalError('workspace write failed')

    monkeypatch.setattr(workspace_db.WorkspaceStore, 'save_agent_run', fail)
    run_id = app.start({'db_id': 'demo', 'question': 'How many rows?'})
    wait_done(app)
    detail = app.read_run(run_id)
    assert detail['task']['state'] == 'completed'
    assert detail['task']['persistence_error'] == 'workspace write failed'
    assert 'persistence_error' not in saved_task(app, run_id)
    stored = WorkspaceStore(database).get_agent_run(run_id)
    assert stored.state == 'running'
    assert stored.result_status is None
    restarted = live_ui.Observatory(app.data, app.output, workspace_db=database)
    assert restarted.read_run(run_id)['task']['state'] == 'completed'
    assert 'persistence_error' not in restarted.read_run(run_id)['task']
    assert WorkspaceStore(database).get_agent_run(run_id).state == 'running'
    assert app.business_path.read_bytes() == app.business_bytes
