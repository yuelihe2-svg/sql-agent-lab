"""Studio page, safe result view, and an offline conversation through the real server."""
import json
from pathlib import Path
import pytest

from sql_agent_lab.workspace.result_view import VIEW_FIELDS, public_result

from test_conversations_api import build, client_for, wait_done


@pytest.mark.parametrize(('value', 'display'), [
    (1234567, '1234567'), (123456789.123456, '123456789.123456'),
    (9223372036854775807, '9223372036854775807'),
])
def test_scalar_precision_survives_summary_and_browser_json(value, display):
    view = public_result(None, state='completed', stage='finished', result={
        'status': 'completed', 'sql': 'SELECT n FROM numbers',
        'feedback': {'status': 'ok', 'columns': ['n'], 'column_count': 1,
                     'preview_rows': [[value]], 'has_more': False, 'truncated': False}})
    assert display in view['summary']
    expected = display if value > 2**53 - 1 else value
    assert view['rows'] == [[expected]]


def test_result_view_keeps_preview_separate_and_drops_private_fields(tmp_path):
    trace = tmp_path / 'trace.jsonl'
    trace.write_text('\n'.join([
        json.dumps({'event': 'call_started', 'messages': ['PROMPT', 'SECRET_SCHEMA'],
                    'prompt': 'D:/SECRET_DB_PATH/business.sqlite'}),
        json.dumps({'event': 'call_completed', 'input_tokens': 3, 'output_tokens': 4,
                    'usage_complete': True, 'content': 'SELECT gold FROM hidden'}),
    ]), encoding='utf8')
    loaded = {
        'status': 'completed',
        'sql': 'SELECT n FROM numbers',
        'score': None,
        'elapsed_seconds': 1.25,
        'feedback': {
            'status': 'ok', 'columns': ['n'], 'column_count': 1,
            'preview_rows': [[1], [2]], 'has_more': True, 'truncated': True,
        },
        'reference_sql': 'SELECT gold FROM hidden',
        'schema_sample': ['SECRET_SCHEMA'],
        'path': 'D:/SECRET_DB_PATH/business.sqlite',
        'events': [{'prompt': 'hidden'}],
    }
    view = public_result(
        tmp_path, state='completed', stage='finished', result=loaded,
        task_error='open failed at D:/SECRET_DB_PATH/business.sqlite')
    body = json.dumps(view)
    assert set(view) == set(VIEW_FIELDS)
    assert view['complete'] is False
    assert view['preview'] is True
    assert view['has_more'] is True
    assert '不能把预览行数当作总数。' in view['summary']
    assert view['evaluated'] is False
    assert view['tokens'] == {'input': 3, 'output': 4}
    assert view['rows'] == [[1], [2]]
    for hidden in ('SECRET_DB_PATH', 'business.sqlite', 'gold', 'PROMPT', 'SECRET_SCHEMA', 'reference_sql'):
        assert hidden not in body


@pytest.mark.parametrize('overrides', [
    {'preview_rows': None}, {'preview_rows': [], 'has_more': None},
    {'preview_rows': [['x' * 170]]}, {'preview_rows': [[2]], 'truncated': 'false'},
    {'preview_rows': [[2]], 'column_count': 3},
])
def test_partial_or_legacy_feedback_is_never_declared_complete(overrides):
    feedback = {'status': 'ok', 'columns': ['n'], 'column_count': 1,
                'has_more': False, 'truncated': False, **overrides}
    view = public_result(None, state='completed', stage=None, result={'sql': 'SELECT n', 'feedback': feedback})
    assert not view['complete']
    assert '没有匹配的记录' not in view['summary']


def test_empty_alias_preserves_later_column_alignment():
    view = public_result(None, state='completed', stage=None, result={
        'sql': 'SELECT 2 AS "", 3 AS n', 'feedback': {'status': 'ok', 'columns': ['', 'n'],
            'column_count': 2, 'preview_rows': [[2, 3]], 'has_more': False, 'truncated': False}})
    assert view['columns'] == ['第1列', 'n']
    assert view['rows'] == [[2, 3]]


def test_successful_value_is_not_reported_as_accuracy(tmp_path):
    view = public_result(tmp_path, state='completed', stage='finished', result={
        'status': 'completed', 'sql': 'SELECT n FROM numbers', 'score': None,
        'feedback': {'status': 'ok', 'columns': ['n'], 'column_count': 1,
                     'preview_rows': [[42]], 'has_more': False, 'truncated': False},
    })
    assert view['evaluated'] is False
    assert view['complete'] is True
    assert view['summary'].startswith('「n」的查询结果是 42。')
    assert '筛选条件' in view['summary']
    assert 'score' not in view
    assert '100' not in json.dumps(view)


def test_interrupted_model_call_cannot_report_partial_usage_as_total(tmp_path):
    trace = tmp_path / 'trace.jsonl'
    trace.write_text('\n'.join(map(json.dumps, [
        {'event': 'call_started'}, {'event': 'call_completed', 'input_tokens': 4, 'output_tokens': 2},
        {'event': 'call_started'}])), encoding='utf8')
    view = public_result(tmp_path, state='interrupted', stage='decomposer', result=None)
    assert view['tokens'] == {'input': None, 'output': None}


def test_single_question_page_receives_only_display_fields(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    def behavior(config, question):
        config.trace({'event': 'call_started', 'role': 'decomposer', 'messages': ['private prompt']})
        for _ in range(105):
            config.trace({'event': 'schema_loaded', 'samples': ['private schema sample']})
        config.trace({'event': 'call_completed', 'role': 'decomposer', 'input_tokens': 3, 'output_tokens': 4,
                      'response': 'private response'})
        return 'SELECT 9223372036854775807 AS n'
    app.behavior = behavior
    run_id = app.start({'db_id': 'shop', 'question': 'Show the identifier'})
    wait_done(app)
    with client_for(app) as client:
        raw = client.get('/api/run', params={'id': run_id})
        compact = client.get('/api/user/run', params={'id': run_id})
        assert raw.status_code == compact.status_code == 200
        view = compact.json()
        assert len(view['events']) == 100 < view['event_count']
        assert view['usage'] == {'calls': 1, 'tokens': {'input': 3, 'output': 4}, 'simulated': False}
        assert view['result']['feedback']['preview_rows'] == [['9223372036854775807']]
        assert len(compact.content) < len(raw.content)
        for marker in ('private prompt', 'private response', 'private schema sample'):
            assert marker in raw.text
            assert marker not in compact.text
        assert [r['id'] for r in client.get('/api/user/runs').json()['runs']] == [run_id]


def test_workspace_page_uses_live_results_and_keeps_old_pages(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    app.behavior = lambda config, question: 'SELECT n FROM numbers'
    with client_for(app) as client:
        studio = client.get('/workspace/').text
        script = client.get('/workspace/app.js').text
        assert 'SQL 智能问答工作区' in studio
        assert '1284600' not in studio and '1284600' not in script
        assert 'innerHTML' not in script
        assert '让数据，回答你的问题' in client.get('/').text
        assert '观察台' in client.get('/observatory').text
        created = client.post('/api/conversations', json={'datasource_id': 'shop', 'title': 'Orders'}).json()
        app.worker_done.clear()
        first = client.post(f"/api/conversations/{created['id']}/messages",
                            json={'question': 'How many rows?'}).json()
        wait_done(app)
        app.worker_done.clear()
        second = client.post(f"/api/conversations/{created['id']}/messages",
                             json={'question': 'How many are left?'}).json()
        wait_done(app)
        detail = client.get(f"/api/conversations/{created['id']}").json()
        assert [item['id'] for item in detail['runs']] == [first['id'], second['id']]
        assert len(client.get('/api/conversations').json()['conversations']) == 1
        view = detail['runs'][0]['result']
        assert view['sql'] == 'SELECT n FROM numbers'
        assert view['columns'] == ['n']
        assert view['rows'] == [[42]]
        assert view['complete'] is True
        assert view['evaluated'] is False
        assert 'events' not in view
        developer = client.get('/api/run', params={'id': first['id']}).json()
        assert 'events' in developer
        body = json.dumps(detail)
        assert 'SECRET_DB_PATH' not in body
        assert 'business.sqlite' not in body
        assert all('SELECT n FROM numbers' not in item['body'] for item in detail['messages'])

        other = client.post('/api/conversations', json={'datasource_id': 'archive'}).json()
        app.worker_done.clear()
        client.post(f"/api/conversations/{other['id']}/messages", json={'question': 'Count archive'})
        wait_done(app)
        shop = client.get(f"/api/conversations/{created['id']}").json()
        archive = client.get(f"/api/conversations/{other['id']}").json()
        assert shop['runs'][0]['result']['rows'] == [[42]]
        assert archive['runs'][0]['result']['rows'] == [[7]]
        assert archive['conversation']['datasource_id'] == 'archive'
        assert all(item['datasource_id'] == 'shop' for item in shop['runs'])

    restarted = __import__('sql_agent_lab.macsql.live_ui', fromlist=['live_ui']).Observatory(
        None, app.output, sources=tmp_path / 'sources.json', workspace_db=app.workspace_db)
    with client_for(restarted) as client:
        detail = client.get(f"/api/conversations/{created['id']}").json()
        assert [item['body'] for item in detail['messages']] == ['How many rows?', 'How many are left?']
        assert detail['runs'][0]['result']['rows'] == [[42]]


def test_stop_failure_and_interrupted_views(tmp_path, monkeypatch):
    import threading
    from sql_agent_lab.macsql import live_ui
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
        paused = live_ui.Observatory(
            None, app.output, sources=tmp_path / 'sources.json', workspace_db=app.workspace_db)
        with client_for(paused) as reader:
            interrupted = reader.get(f"/api/conversations/{created['id']}").json()
            assert interrupted['runs'][0]['state'] == 'interrupted'
            assert interrupted['runs'][0]['result']['sql'] is None
            assert 'SECRET_DB_PATH' not in json.dumps(interrupted)
        assert client.post('/api/stop', json={'id': started.json()['id']}).json()['status'] == 'stop_requested'
        release.set()
        wait_done(app)
        stopped = client.get(f"/api/conversations/{created['id']}").json()
        assert stopped['runs'][0]['state'] == 'stopped'
        assert stopped['runs'][0]['result']['summary'] is None

        app.behavior = lambda config, question: 'SELECT missing_column FROM numbers'
        app.worker_done.clear()
        failed = client.post(f"/api/conversations/{created['id']}/messages",
                             json={'question': 'This SQL fails'})
        assert failed.status_code == 200
        wait_done(app)
        detail = client.get(f"/api/conversations/{created['id']}").json()
        failure = detail['runs'][1]['result']
        assert detail['runs'][1]['state'] == 'failed'
        assert failure['complete'] is False
        assert '未找到对应的表或字段' in failure['summary']
        assert failure['rows'] == []
        assert 'missing_column' in failure['sql']
    assert all(path.read_bytes() == content for path, content in app.business_before.items())


def test_workspace_off_serves_the_page_without_changing_start(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, workspace=False)
    with client_for(app) as client:
        assert client.get('/api/conversations').status_code == 404
        page = client.get('/workspace/').text
        assert 'SQL 智能问答工作区' in page
        assert '未启用工作区' in client.get('/workspace/app.js').text
        assert client.get('/').status_code == 200
        app.worker_done.clear()
        started = client.post('/api/start', json={'db_id': 'shop', 'question': 'How many rows?'})
        assert started.status_code == 200
        wait_done(app)
        assert client.get('/api/run', params={'id': started.json()['id']}).json()['task']['state'] == 'completed'
    assert list(Path(tmp_path).rglob('workspace.sqlite')) == []
