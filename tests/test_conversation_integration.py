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
