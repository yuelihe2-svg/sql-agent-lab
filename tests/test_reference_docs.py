"""Exercise document QA through the real workspace routes with local fixtures."""
import hashlib
import json
from uuid import uuid4

import pytest

from sql_agent_lab.macsql.live_ui import Observatory
from sql_agent_lab.workspace.reference_docs import reference_answer, validate_reference
from test_conversations_api import build, client_for


def test_accounts_citations_versions_and_source_scope(tmp_path, monkeypatch):
    admin = build(tmp_path, monkeypatch)
    payload = {'title': '售后政策.md', 'format': 'markdown', 'body': '# 退货\n寄回运费由商家承担。'}
    doc = admin.add_source_knowledge('shop', payload)
    admin.add_source_knowledge('archive', {**payload, 'body': '# 退货\n寄回运费由顾客承担。'})
    config = tmp_path / 'accounts.json'
    tokens = ['A' * 48, 'B' * 48]
    config.write_text(json.dumps([dict(id=str(uuid4()), name=src, sources=[src],
        token_sha256=hashlib.sha256(token.encode()).hexdigest()) for src, token in zip(('shop', 'archive'), tokens)]))
    app = Observatory(None, admin.output, sources=tmp_path/'sources.json', workspace_db=admin.workspace_db, accounts_config=config)
    monkeypatch.setattr('sql_agent_lab.macsql.live_ui.LocalOllamaModel', lambda *a, **kw: pytest.fail('unexpected inference'))
    with client_for(app) as a, client_for(app) as b:
        for client, src, token, expected in zip((a,b), ('shop','archive'), tokens, ('商家','顾客')):
            client.post('/api/session', json={'token':token}).raise_for_status()
            cid = client.post('/api/conversations', json={'datasource_id':src}).json()['id']
            chat = '/api/conversations/' + cid
            client.post(chat+'/messages', json={'question':'根据售后政策，寄回运费由谁承担？'}).raise_for_status()
            import time
            for _ in range(100):
                runs = client.get(chat).json()['runs']
                view = runs[-1]['result'] if runs else {}
                if view and view.get('state') == 'completed':
                    break
                time.sleep(.05)
            assert expected in view['summary'] and not view['sql']
            cite, = view['citations']
            assert cite['datasource_id'] == src and '第 2 行' in cite['location']
        link = '/api/sources/shop/knowledge/'+doc['id']+'?version=1&passage=0'
        assert '寄回运费由商家承担' in a.get(link).text
        assert b.get(link).status_code == 404
        admin.update_source_knowledge('shop',doc['id'],{**payload,'body':'运费按实报销','expected_version':1})
        assert a.get(link).status_code == 409
        admin.delete_source_knowledge('shop',doc['id'])
        assert a.get(link.replace('version=1','version=2')).is_error


def test_json_locations_and_unanswered_questions():
    doc = dict(title='退款字典.json',format='json',body='{"退款状态":{"P1":"等待复核","R2":"退回申请"}}',datasource_id='shop',id='x',version=1)
    answer = reference_answer('字典里 P1 是什么意思？',lambda:[doc])
    assert len(answer['citations']) == 1
    assert answer['citations'][0]['location'] == '/退款状态/P1'
    assert '没有找到' in reference_answer('资料里有停车补贴吗？',lambda:[doc])['answer']
    assert reference_answer('按资料统计今年订单数',lambda:[doc]) is None
    with pytest.raises(ValueError):
        validate_reference('订单.json','json','[{"id":1}]')


def test_comparing_codes_across_separate_json_paths_requires_all_evidence():
    doc = dict(title='状态.json', format='json', body='{"P1":"待审核","R2":"已退回","S3":"已结清"}',
               datasource_id='store', id='doc', version=1)
    view = reference_answer('字典中 P1 和 R2 有什么区别？', lambda: [doc])
    assert {c['location'] for c in view['citations']} == {'/P1', '/R2'}
    missing = reference_answer('字典中 P1 和 Z9 有什么区别？', lambda: [doc])
    assert 'Z9' in missing['answer'] and not missing.get('citations')

@pytest.mark.parametrize("question", [
    "按政策，本月一共多少笔订单？",
    "根据手册查询2025年各渠道的总金额。",
])
def test_measured_totals_stay_on_data_route(question):
    def unexpected_lookup():
        pytest.fail("A data question should not read reference files")
    assert reference_answer(question, unexpected_lookup) is None


def test_natural_policy_questions_and_passage_specificity():
    doc = dict(title='采购规范.md', format='markdown', datasource_id='采购', id='policy', version=1,
               body='# 采购规则\n本规范生效日期为2025年8月1日。\n## 退货\n定制商品不能无理由退货。')
    for q, expected in [('采购规则从何时开始实施？', 0), ('定制商品能无理由退货吗？', 1)]:
        result = reference_answer(q, lambda: [doc])
        assert [c['passage'] for c in result['citations']] == [expected]

def test_permission_and_data_routes_in_workspace(tmp_path, monkeypatch):
    import sqlite3
    from contextlib import closing
    from test_conversations_api import wait_done

    app = build(tmp_path, monkeypatch)
    with closing(sqlite3.connect(app.databases['shop'])) as db:
        db.executescript('CREATE TABLE orders(amount INTEGER); INSERT INTO orders VALUES (40),(70);')
    app.add_source_knowledge('shop', {'title': '培训资料.md', 'format': 'markdown',
        'body': '# 培训事务\n缺席者不发结业证明。\n外部讲师借用投影仪须登记。'})
    candidates = {'本店一共有几笔订单？': 'SELECT COUNT(*) FROM orders',
                  '可以看看本店订单总金额吗？': 'SELECT SUM(amount) FROM orders'}
    calls = []
    def scripted_sql(config, question):
        calls.append(question.question)
        return candidates[question.question]
    app.behavior = scripted_sql
    def ask(text):
        cid = app.create_conversation({'datasource_id': 'shop'})['id']
        app.worker_done.clear()
        app.post_conversation_message(cid, {'question': text})
        wait_done(app)
        return app.conversation_detail(cid)['runs'][-1]['result']

    answer = ask('缺席还能拿到结业证明吗？')
    assert answer['citations'][0]['passage'] == 0 and not answer['sql']
    for text in ['旁听生能拿到结业证明吗？', '外部讲师可以报销投影仪吗？']:
        view = ask(text)
        assert view['needs_clarification'] and not view['citations'] and not view['sql']
    assert calls == []
    for text, value in zip(candidates, (2, 110)):
        view = ask(text)
        assert view['rows'] == [[value]] and view['sql'] == candidates[text]
    assert calls == list(candidates)

def test_permission_does_not_drop_time_or_negation():
    doc = dict(title='场馆.md', format='markdown', id='venue', version=1, datasource_id='test',
               body='入场前可以换证。\n未完成补课者不发结业证明。')
    for question in ['入场后可以换证吗？', '完成补课后能拿到结业证明吗？']:
        assert reference_answer(question, lambda: [doc]).get('clarification')
