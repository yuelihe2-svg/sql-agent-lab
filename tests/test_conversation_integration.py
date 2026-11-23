from test_conversations_api import build, client_for, wait_done
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent, _messages
from sql_agent_lab.macsql.reference_core import ModelReply
import pytest


def test_history_reaches_script_model_and_is_isolated(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    calls = []

    def scripted(**kwargs):
        calls.append(kwargs['messages'])
        return ModelReply(chr(96)*3 + 'sql\nSELECT n FROM numbers\n' + chr(96)*3, 1, 1, simulated=True)

    from dataclasses import replace
    def factory(models, *, config):
        return ReferenceMacSQLAgent(models, config=replace(config, model=scripted))
    monkeypatch.setattr('sql_agent_lab.macsql.live_ui.ReferenceMacSQLAgent', factory)
    with client_for(app) as client:
        def conversation(source):
            return client.post('/api/conversations', json={'datasource_id': source}).json()['id']
        first, other, archive = conversation('shop'), conversation('shop'), conversation('archive')
        def ask(cid, question, **options):
            calls.clear()
            app.worker_done.clear()
            response = client.post(f'/api/conversations/{cid}/messages',
                                   json={'question': question, **options})
            assert response.status_code == 200, response.text
            wait_done(app)
            assert calls
            return calls[0]
        original = ask(first, 'Count numbers for January')
        assert len(original) == 3
        followup = ask(first, 'And February?')
        assert 'Count numbers for January' in followup[-1]['content']
        assert followup[-1]['role'] == 'user'
        assert 'SELECT n FROM numbers' in followup[-1]['content']
        assert 'Count numbers for January' not in followup[0]['content']
        assert 'And February?' in followup[1]['content']
        assert len(ask(first, 'Explicit opt out', use_grounding=False, use_conversation_context=False)) == 2
        assert len(ask(other, 'And February?', use_conversation_context=True)) == 3
        assert len(ask(archive, 'And February?', use_conversation_context=True)) == 3
        empty = conversation('shop')
        calls.clear()
        app.worker_done.clear()
        response = client.post(f'/api/conversations/{empty}/messages', json={'question': '那杭州呢？'})
        assert response.status_code == 200
        wait_done(app)
        assert not calls
        detail = app.conversation_detail(empty)
        assert '补充完整条件' in detail['runs'][-1]['result']['summary']
        assert detail['runs'][-1]['result']['sql'] is None
        resumed = ask(empty, '统计2024年已付款订单总金额')
        assert '原问题：那杭州呢？' in resumed[1]['content']
        assert '用户补充：统计2024年已付款订单总金额' in resumed[1]['content']
        assert client.post(f'/api/conversations/{first}/messages', json={
            'question': 'test', 'use_conversation_context': 'true'}).status_code == 400


def test_history_is_counted_by_final_policy(tmp_path):
    from sql_agent_lab.macsql.context import ContextBudgetExceeded, ContextPolicy
    from sql_agent_lab.macsql.reference_core import WorkflowConfig, ROLES
    agent = ReferenceMacSQLAgent({role: 'offline' for role in ROLES},
        config=WorkflowConfig(None, None, context=ContextPolicy()))
    agent.conversation_block = 'x' * 30000
    with pytest.raises(ContextBudgetExceeded):
        agent._fit('decomposer', lambda schema: 'Current question', {})
    assert _messages('prompt') == _messages('prompt', '', '')


@pytest.mark.parametrize('question', ['那杭州呢？', '改成2023年呢？', '统计订单'])
def test_clarification_works_without_ollama(tmp_path, monkeypatch, question):
    app = build(tmp_path, monkeypatch)
    attempted = []

    def offline(*args, **kwargs):
        attempted.append(True)
        raise RuntimeError('Ollama unavailable')

    monkeypatch.setattr('sql_agent_lab.macsql.live_ui.LocalOllamaModel', offline)
    cid = app.create_conversation({'datasource_id': 'shop'})['id']
    app.post_conversation_message(cid, {'question': question})
    wait_done(app)
    result = app.conversation_detail(cid)['runs'][0]['result']
    assert '补充完整条件' in result['summary']
    assert result['sql'] is None
    assert not attempted


def test_web_uses_the_actual_graph_result_without_reexecuting_sql(tmp_path, monkeypatch):
    from dataclasses import replace
    from sql_agent_lab.macsql import live_ui
    from sql_agent_lab.macsql.tools import SQLiteTools
    app = build(tmp_path, monkeypatch)
    calls = []
    execute = SQLiteTools.execute_sql_readonly
    def counted(self, db_id, sql):
        calls.append(sql)
        return execute(self, db_id, sql)

    def model(**kwargs):
        return ModelReply('```sql\nSELECT n FROM numbers\n```', 12, 7, simulated=True)

    def no_file_scan(*args):
        pytest.fail('A standalone query must not scan the database file')

    monkeypatch.setattr(live_ui, 'sha256', no_file_scan)
    monkeypatch.setattr(SQLiteTools, 'execute_sql_readonly', counted)
    monkeypatch.setattr(live_ui, 'ReferenceMacSQLAgent', lambda models, config:
                        ReferenceMacSQLAgent(models, config=replace(config, model=model)))
    cid = app.create_conversation({'datasource_id': 'shop'})['id']
    app.post_conversation_message(cid, {'question': 'What number is stored?'})
    wait_done(app)
    run = app.conversation_detail(cid)['runs'][0]
    assert calls == ['SELECT n FROM numbers']
    assert run['result']['rows'] == [[42]]
    assert run['result']['tokens'] == {'input': 12, 'output': 7}
    # A completed chat can display its usage without historical trace access.
    (app.output / run['id'] / 'trace.jsonl').unlink()
    assert app.conversation_detail(cid)['runs'][0]['result'] == run['result']
