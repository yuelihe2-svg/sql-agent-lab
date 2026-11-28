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
