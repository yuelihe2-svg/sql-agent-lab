import base64
import pytest
from test_conversations_api import build, client_for, database, wait_done
from sql_agent_lab.macsql.live_ui import Observatory


def test_empty_workspace_accepts_first_database_and_reopens(tmp_path):
    workspace = tmp_path / 'workspace.sqlite'
    output = tmp_path / 'runs'
    app = Observatory(None, output, workspace_db=workspace)
    business = tmp_path / 'shop.sqlite'
    database(business, 42)
    with client_for(app) as client:
        assert client.get('/api/catalog').json()['sources'] == []
        assert client.get('/api/conversations').json()['conversations'] == []
        uploaded = client.post('/api/sources/upload', json={
            'name': '第一家商店', 'data': base64.b64encode(business.read_bytes()).decode()})
        assert uploaded.status_code == 200, uploaded.text
        source = uploaded.json()['source']
        response = client.post('/api/conversations', json={'datasource_id': source['id']})
        assert response.status_code == 200, response.text
    restored = Observatory(None, output, workspace_db=workspace)
    assert restored.catalog()['sources'] == [source]
    assert restored.list_conversations()['conversations'][0]['id'] == response.json()['id']


def test_upload_restart_and_description(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    data = next(iter(app.databases.values())).read_bytes()
    with client_for(app) as client:
        bad = client.post('/api/sources/upload', json={'name': 'bad', 'data': base64.b64encode(b'bad').decode()})
        assert bad.status_code == 400
        response = client.post('/api/sources/upload', json={'name': '新数据库', 'data': base64.b64encode(data).decode(), 'description': '金额单位是元'})
        assert response.status_code == 200, response.text
        sid = response.json()['source']['id']
        assert 'path' not in response.text
        cid = client.post('/api/conversations', json={'datasource_id': sid}).json()['id']
        def forbidden(*args, **kwargs):
            raise AssertionError('metadata intent must not create model')
        monkeypatch.setattr('sql_agent_lab.macsql.live_ui.LocalOllamaModel', forbidden)
        app.worker_done.clear()
        sent = client.post(f'/api/conversations/{cid}/messages', json={'question': '描述一下这个数据库'})
        assert sent.status_code == 200
        wait_done(app)
        result = app.conversation_detail(cid)['runs'][-1]['result']
        assert '张表' in result['summary']
        assert result['sql'] is None
    restored = Observatory(None, app.output, workspace_db=app.workspace_db)
    assert sid in restored.databases
    assert restored.list_source_knowledge(sid)


def test_business_definition_and_sql_route():
    from sql_agent_lab.workspace.chat_intent import direct_answer
    def no_profile():
        raise AssertionError('not metadata')
    assert '没有业务说明' in direct_answer('已付款怎么定义？', no_profile, lambda: [])
    assert 'paid' in direct_answer('已付款怎么定义？', no_profile, lambda: [{'title': '付款', 'body': 'paid 表示已付款'}])
    assert direct_answer('统计已付款金额是多少', no_profile) is None
    docs = [{'title': f'库存{i}', 'body': '库存按件计算'} for i in range(4)]
    docs.append({'title': '已付款', 'body': '仅包含 paid 状态'})
    answer = direct_answer('什么是已付款？', no_profile, lambda: docs)
    assert 'paid' in answer and '库存' not in answer
    assert '没有找到' in direct_answer('退款是什么意思？', no_profile, lambda: docs)
    assert direct_answer('按业务说明统计上海订单金额', no_profile, lambda: docs) is None


def test_upload_registry_failure_can_be_retried_cleanly(tmp_path, monkeypatch):
    from sql_agent_lab.macsql import live_ui
    from sql_agent_lab.workspace.database import LOCAL_WORKSPACE_ID
    app = build(tmp_path, monkeypatch)
    store = app._workspace()
    before = app.catalog()
    source_data = next(iter(app.databases.values())).read_bytes()
    payload = {'name': '门店', 'data': base64.b64encode(source_data).decode(), 'description': '按元计价'}
    with monkeypatch.context() as fault:
        def fail(path, value):
            raise OSError('injected registry write failure')
        fault.setattr(live_ui, 'write_json_atomic', fail)
        with pytest.raises(OSError):
            app.upload_database(payload)
    assert app.catalog() == before
    assert [s.id for s in store.list_data_sources(LOCAL_WORKSPACE_ID)] == before['databases']
    assert store.list_all_knowledge_rows() == []
    assert not list((app.output / 'databases').iterdir())
    assert not app.upload_registry.exists()
    source = app.upload_database(payload)['source']
    assert app.list_source_knowledge(source['id'])['knowledge'][0]['body'] == payload['description']


def test_knowledge_http_accepts_full_unicode_character_limit(tmp_path, monkeypatch):
    import json
    app = build(tmp_path, monkeypatch)
    note = {'title': '说明', 'format': 'text', 'body': '📚' * 24000}
    with client_for(app) as client:
        first = client.post('/api/sources/shop/knowledge', content=json.dumps(note))
        assert first.status_code == 200, first.text[:200]
        updated = client.put('/api/sources/shop/knowledge/' + first.json()['id'], content=json.dumps(note))
        assert updated.status_code == 200 and updated.json()['body'] == note['body']
        for option in ('retrieval', 'semantic_review'):
            response = client.post('/api/start', json={'db_id': 'shop', 'question': '测试', option: 'false'})
            assert response.status_code == 400
        assert app.active is None
