"""Offline scheduler tests across HTTP, disk recovery, and model transport."""
import time
import threading

import httpx
import pytest

from sql_agent_lab.macsql import live_ui
from sql_agent_lab.macsql.task_lifecycle import change_task, write_json_atomic
from test_conversations_api import build, client_for


def wait_for_idle(app):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with app.lock:
            if app.active is None and not app.pending:
                return
        time.sleep(0.01)
    pytest.fail("Scheduler did not become idle")


def test_http_queue_fifo_capacity_and_targeted_cancel(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    app.queue_limit = 2
    entered, release = threading.Event(), threading.Event()
    calls = []

    def agent(config, question):
        calls.append(question.question)
        if question.question == 'first':
            entered.set()
            assert release.wait(5)
        return 'SELECT n FROM numbers'

    app.behavior = agent
    with client_for(app) as client:
        cid = client.post('/api/conversations', json={'datasource_id': 'shop'}).json()['id']
        path = f'/api/conversations/{cid}'

        def send(text):
            response = client.post(path + '/messages', json={'question': text})
            assert response.is_success, response.text
            return response.json()['id']

        first = send('first')
        try:
            assert entered.wait(5)
            discarded, second = send('cancel this'), send('second')
            detail = client.get(path).json()
            assert [(r['id'], r['state']) for r in detail['runs']] == [
                (first, 'running'), (discarded, 'queued'), (second, 'queued')]
            assert detail['runs'][2]['started_at'] is None
            earlier = app._workspace().recent_message_runs(cid, exclude_run_id=first, limit=32)
            assert earlier == []
            overflow = client.post(path + '/messages', json={'question': 'queue is full'})
            assert overflow.status_code == 400 and '队列已满' in overflow.json()['error']
            assert client.post('/api/stop', json={'id': discarded}).json()['status'] == 'cancelled'
            third = send('third')
            assert client.post('/api/stop', json={'id': discarded}).json()['status'] == 'not_active'
            assert calls == ['first']
        finally:
            release.set()
        wait_for_idle(app)
        assert calls == ['first', 'second', 'third']
        after = client.get(path).json()
        assert [(r['id'], r['state']) for r in after['runs']] == [
            (first, 'completed'), (discarded, 'stopped'), (second, 'completed'), (third, 'completed')]
        assert after['runs'][1]['result']['sql'] is None


def test_execution_budget_releases_worker_and_drops_late_result(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def agent(config, question):
        if question.question == 'slow':
            entered.set()
            assert release.wait(5)
        return 'SELECT n FROM numbers'

    app.behavior = agent
    cid = app.create_conversation({'datasource_id': 'shop'})['id']
    app.post_conversation_message(cid, {'question': 'slow'})
    try:
        assert entered.wait(5)
        app.post_conversation_message(cid, {'question': 'next'})
        with app.lock:
            app.stop.deadline = time.monotonic() - 1
    finally:
        release.set()
    wait_for_idle(app)
    first, second = app.conversation_detail(cid)['runs']
    assert first['state'] == 'failed'
    assert '超时' in first['result']['error']
    assert first['result']['sql'] is None and first['result']['rows'] == []
    assert second['state'] == 'completed' and second['result']['rows'] == [[42]]


def test_recovery_resumes_only_waiting_tasks_without_duplicate_messages(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    monkeypatch.setattr(app, '_dispatch_pending', lambda **kwargs: None)
    cid = app.create_conversation({'datasource_id': 'shop'})['id']

    def enqueue(text):
        return app.post_conversation_message(cid, {'question': text})['id']

    abandoned, cancelled, first, second = map(enqueue, ['abandoned', 'cancelled', 'first', 'second'])
    write_json_atomic(app.output / abandoned / 'task.json', change_task(app.tasks[abandoned], state='running'))
    app.request_stop(cancelled)
    restarted = live_ui.Observatory(None, app.output, sources=tmp_path / 'sources.json',
                                   workspace_db=app.workspace_db)
    calls = []

    def agent(config, question):
        calls.append(question.question)
        return 'SELECT n FROM numbers'

    app.behavior = agent
    assert restarted.read_run(abandoned)['task']['state'] == 'interrupted'
    assert list(restarted.pending) == [first, second]
    restarted.resume_pending()
    wait_for_idle(restarted)
    detail = restarted.conversation_detail(cid)
    assert calls == ['first', 'second']
    assert [m['body'] for m in detail['messages']] == ['abandoned', 'cancelled', 'first', 'second']
    assert [r['state'] for r in detail['runs']] == ['interrupted', 'stopped', 'completed', 'completed']
    fresh = live_ui.Observatory(None, app.output, sources=tmp_path / 'sources.json', workspace_db=app.workspace_db)
    assert not fresh.pending


def test_local_request_uses_remaining_budget_without_network(monkeypatch):
    clock = iter([100, 100.25, 101])
    monkeypatch.setattr(live_ui.time, 'monotonic', lambda: next(clock))
    stop = live_ui.RunStop()
    stop.deadline = 101
    request = httpx.Request('POST', 'http://127.0.0.1:11434/api/chat')
    stop.limit_request(request)
    assert request.extensions['timeout']['read'] == 0.75
    with pytest.raises(live_ui.Stopped):
        stop.limit_request(request)
