import base64
from test_conversations_api import build, client_for, wait_done
from sql_agent_lab.macsql.live_ui import Observatory


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
