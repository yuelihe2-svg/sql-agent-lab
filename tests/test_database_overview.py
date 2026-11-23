"""数据库介绍 uses the profile and knowledge APIs without changing a query."""
from contextlib import closing
import sqlite3

from sql_agent_lab.workspace.profile import extract_profile

from test_conversations_api import build, client_for, wait_done


def test_empty_catalog_has_no_inferred_relationships(tmp_path):
    database = tmp_path / 'empty.sqlite'
    with closing(sqlite3.connect(database)):
        pass
    before = database.read_bytes()
    profile = extract_profile(database)
    assert profile['tables'] == []
    assert profile['relationships'] == []
    assert profile['coverage'] == {
        'table_count': 0,
        'tables_without_primary_key': [],
        'declared_foreign_key_count': 0,
        'business_definitions': 'not_inferred',
    }
    assert database.read_bytes() == before


def test_overview_script_is_served_and_queries_stay_unchanged(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    with client_for(app) as client:
        script = client.get('/workspace/app.js').text
        assert '数据库介绍' in script
        assert 'innerHTML' not in script
        assert 'storage_key' not in script
        missing = client.get('/api/sources/missing/profile')
        assert missing.status_code == 400
        created = client.post('/api/sources/shop/knowledge', json={
            'title': 'Rules', 'format': 'text', 'body': 'A user definition'})
        updated = client.put(f"/api/sources/shop/knowledge/{created.json()['id']}", json={
            'title': 'Rules', 'format': 'text', 'body': 'A revised user definition'})
        assert updated.json()['version'] == 2
        assert updated.json()['verification'] == 'user_provided_unverified'
        context = client.get('/api/sources/shop/context').json()
        assert context['knowledge'][0]['version'] == 2
        assert client.get('/api/sources/archive/context').json()['knowledge'] == []
        app.worker_done.clear()
        conversation = client.post('/api/conversations', json={'datasource_id': 'shop'}).json()
        posted = client.post(f"/api/conversations/{conversation['id']}/messages",
                             json={'question': 'How many rows?'})
        assert posted.status_code == 200
        wait_done(app)
        detail = client.get(f"/api/conversations/{conversation['id']}").json()
        assert detail['messages'][0]['body'] == 'How many rows?'
        assert 'A revised user definition' not in detail['messages'][0]['body']

    bare = build(tmp_path / 'off', monkeypatch, workspace=False)
    with client_for(bare) as client:
        assert client.get('/api/sources/shop/profile').status_code == 404
        assert client.get('/api/sources/shop/context').status_code == 404
        assert '数据库介绍' in client.get('/workspace/app.js').text
