"""Database profile and optional business knowledge. The agent is not invoked."""
from contextlib import closing
import json
import sqlite3

from sql_agent_lab.macsql import live_ui
from sql_agent_lab.workspace.database import SCHEMA_VERSION, WorkspaceStore, _MIGRATIONS, _connect, migrate
from sql_agent_lab.workspace.profile import extract_profile

from test_conversations_api import build, client_for


def _shop(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT)')
        conn.execute(
            'CREATE TABLE orders('
            'id INTEGER NOT NULL, customer_id INTEGER, '
            'PRIMARY KEY (id), '
            'FOREIGN KEY(customer_id) REFERENCES customers(id))')
        conn.execute('CREATE TABLE notes(body TEXT)')
        conn.execute("INSERT INTO customers VALUES (1, 'credential-sentinel-value')")
        conn.execute('INSERT INTO orders VALUES (7, 1)')
        conn.commit()
    return path


def test_profile_reads_keys_without_copying_rows_or_paths(tmp_path):
    database = _shop(tmp_path / 'SECRET_DB_PATH' / 'business.sqlite')
    before = database.read_bytes()
    first = extract_profile(database)
    second = extract_profile(database)
    assert first['fingerprint'] == second['fingerprint']
    assert len(first['fingerprint']) == 64
    orders = next(table for table in first['tables'] if table['name'] == 'orders')
    assert orders['primary_key'] == ['id']
    assert orders['columns'][1] == {'name': 'customer_id', 'type': 'INTEGER', 'primary_key': False}
    assert orders['foreign_keys'] == [{
        'from_table': 'orders', 'from_columns': ['customer_id'],
        'to_table': 'customers', 'to_columns': ['id'], 'origin': 'declared_foreign_key',
    }]
    assert first['coverage']['tables_without_primary_key'] == ['notes']
    assert first['coverage']['business_definitions'] == 'not_inferred'
    body = json.dumps(first)
    assert 'credential-sentinel-value' not in body
    assert 'SECRET_DB_PATH' not in body
    assert 'business.sqlite' not in body
    assert database.read_bytes() == before
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("INSERT INTO notes VALUES ('another-sentinel')")
        conn.commit()
    assert extract_profile(database)['fingerprint'] == first['fingerprint']


def test_version_one_store_upgrades_in_place(tmp_path):
    path = tmp_path / 'workspace.sqlite'
    with closing(_connect(path)) as conn:
        conn.execute(
            'CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        conn.executescript(_MIGRATIONS[0][1])
        conn.execute(
            'INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)',
            ('2026-11-18T00:00:00+00:00',))
        conn.execute(
            'INSERT INTO workspace(id, name, created_at, local_only) VALUES (?, ?, ?, 1)',
            ('5c6a1e0a-7b34-4d5e-9f10-0a1b2c3d4e5f', 'Local', '2026-11-18T00:00:00+00:00'))
        conn.commit()
    migrate(path)
    with closing(_connect(path)) as conn:
        versions = [row[0] for row in conn.execute('SELECT version FROM schema_migrations ORDER BY version')]
        assert versions == list(range(1, SCHEMA_VERSION + 1))
        assert conn.execute('SELECT name FROM workspace').fetchone()[0] == 'Local'
        assert conn.execute(
            "SELECT name FROM sqlite_schema WHERE name = 'knowledge_document'").fetchone() is not None
    business = tmp_path / 'business.sqlite'
    before = _shop(business).read_bytes()
    migrate(path)
    assert business.read_bytes() == before


def test_knowledge_is_versioned_isolated_and_absent_from_the_agent(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path, content in app.business_before.items()}
    with client_for(app) as client:
        assert client.get('/api/sources/missing/profile').status_code == 400
        profile = client.get('/api/sources/shop/profile').json()
        assert profile['datasource_id'] == 'shop'
        assert profile['schema_facts'] == 'catalog'
        assert profile['business_definitions'] == 'not_inferred'
        assert profile['tables'][0]['name'] == 'numbers'
        assert profile['tables'][0]['primary_key'] == []
        again = client.get('/api/sources/shop/profile').json()
        assert again['fingerprint'] == profile['fingerprint']
        encoded = json.dumps(profile)
        assert 'SECRET_DB_PATH' not in encoded
        assert 'business.sqlite' not in encoded
        assert 'rows' not in profile
        assert all('values' not in column for table in profile['tables'] for column in table['columns'])
        assert 'storage_key' not in encoded

        rejected = client.post('/api/sources/shop/knowledge', json={
            'title': 'Rules', 'format': 'markdown', 'body': 'Net revenue',
            'path': 'C:/SECRET_DB_PATH/business.sqlite'})
        assert rejected.status_code == 400
        assert client.post('/api/sources/shop/knowledge', json={
            'title': 'Rules', 'format': 'html', 'body': 'Net revenue'}).status_code == 400
        assert client.post('/api/sources/shop/knowledge', json={
            'title': 'Rules', 'format': 'json', 'body': '{'}).status_code == 400
        assert client.post('/api/sources/shop/knowledge', json={
            'title': 'Rules', 'format': 'text', 'body': 'x' * 24001}).status_code == 400

        created = client.post('/api/sources/shop/knowledge', json={
            'title': '../outside',
            'format': 'markdown',
            'body': '# Rules\n\nNet Collected Revenue is a user definition, not a verified fact.',
        })
        assert created.status_code == 200
        document = created.json()
        assert document['version'] == 1
        assert document['provenance'] == 'user'
        assert document['verification'] == 'user_provided_unverified'
        assert 'storage_key' not in document
        assert client.get(f"/api/sources/archive/knowledge/{document['id']}").status_code == 400
        assert client.get('/api/sources/archive/knowledge').json()['knowledge'] == []

        changed = client.put(f"/api/sources/shop/knowledge/{document['id']}", json={
            'title': 'Revenue rules', 'format': 'markdown', 'body': document['body']})
        assert changed.status_code == 200
        assert changed.json()['version'] == 2
        assert changed.json()['content_hash'] == document['content_hash']
        revised = client.put(f"/api/sources/shop/knowledge/{document['id']}", json={
            'title': 'Revenue rules', 'format': 'json',
            'body': '{"metric":"net revenue","verification":"user"}'})
        assert revised.json()['version'] == 3
        assert revised.json()['content_hash'] != document['content_hash']

        context = client.get('/api/sources/shop/context').json()
        assert context['profile']['fingerprint'] == profile['fingerprint']
        assert context['knowledge'][0]['id'] == document['id']
        assert context['business_definitions'] == 'user_provided_unverified'
        assert 'SECRET_DB_PATH' not in json.dumps(context)

        directory = app.workspace_db.parent / (app.workspace_db.stem + '-knowledge')
        stored = list(directory.iterdir())
        assert stored and all(path.parent == directory and path.suffix == '' for path in stored)
        stored[0].write_text('tampered', encoding='utf-8')
        assert client.get(f"/api/sources/shop/knowledge/{document['id']}").status_code == 400
        stored[0].unlink()
        assert client.get(f"/api/sources/shop/knowledge/{document['id']}").status_code == 400
        restored = client.put(f"/api/sources/shop/knowledge/{document['id']}", json={
            'title': 'Revenue rules', 'format': 'json',
            'body': '{"metric":"net revenue","verification":"user"}'})
        assert restored.status_code == 200
        assert restored.json()['version'] == 4

        replaced = client.post('/api/sources/shop/knowledge', json={
            'title': 'Replacement', 'format': 'text',
            'body': 'SECRET_DEFINITION stays out of the agent question'})
        assert replaced.status_code == 200
        app.worker_done.clear()
        started = client.post('/api/conversations', json={'datasource_id': 'shop', 'title': 'Orders'})
        posted = client.post(
            f"/api/conversations/{started.json()['id']}/messages",
            json={'question': 'How many rows?'})
        assert posted.status_code == 200
        from test_conversations_api import wait_done
        wait_done(app)
        manifest = json.loads((app.output / posted.json()['id'] / 'manifest.json').read_text(encoding='utf8'))
        assert manifest['question']['question'] == 'How many rows?'
        assert 'SECRET_DEFINITION' not in json.dumps(manifest)

        removed = client.delete(f"/api/sources/shop/knowledge/{replaced.json()['id']}")
        assert removed.json()['deleted'] is True
        assert client.get(f"/api/sources/shop/knowledge/{replaced.json()['id']}").status_code == 400
        assert client.delete(f"/api/sources/shop/knowledge/{replaced.json()['id']}").status_code == 400

    reopened = WorkspaceStore(app.workspace_db)
    rows = reopened.list_knowledge_rows('shop')
    assert [row['title'] for row in rows] == ['Revenue rules']
    assert rows[0]['version'] == 4
    restarted = live_ui.Observatory(
        None, app.output, sources=tmp_path / 'sources.json', workspace_db=app.workspace_db)
    with client_for(restarted) as client:
        loaded = client.get('/api/sources/shop/knowledge').json()['knowledge']
        assert loaded[0]['body'].startswith('{"metric"')
        assert loaded[0]['version'] == 4
        assert client.get('/api/sources/shop/profile').json()['fingerprint'] == profile['fingerprint']
    assert {path: path.read_bytes() for path in before} == before


def test_profile_routes_stay_hidden_without_a_workspace(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch, workspace=False)
    with client_for(app) as client:
        assert client.get('/api/sources/shop/profile').status_code == 404
        assert client.post('/api/sources/shop/knowledge', json={
            'title': 'Rules', 'format': 'text', 'body': 'optional'}).status_code == 404
        assert client.get('/api/conversations').status_code == 404
        assert 'SQL 智能问答工作区' in client.get('/workspace/').text
    assert list(tmp_path.rglob('workspace.sqlite')) == []
    assert list(tmp_path.rglob('*-knowledge')) == []
