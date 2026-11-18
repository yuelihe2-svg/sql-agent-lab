"""Optional startup registration copies public source metadata and not database files."""
from contextlib import closing, contextmanager
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading

import httpx
import pytest

from sql_agent_lab.macsql import live_ui
from sql_agent_lab.macsql.data_sources import SourceRegistry
from sql_agent_lab.workspace.database import LOCAL_WORKSPACE_ID, WorkspaceStore


@contextmanager
def client_for(app):
    server = ThreadingHTTPServer(('127.0.0.1', 0), live_ui.make_handler(app))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            yield client
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def business_database(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE secrets(password TEXT)')
        conn.execute("INSERT INTO secrets VALUES ('credential-sentinel-value')")
        conn.commit()
    return path


def source_config(tmp_path):
    root = tmp_path / 'SECRET_DB_PATH'
    root.mkdir()
    business_database(root / 'business.sqlite')
    business_database(root / 'archive.sqlite')
    config = tmp_path / 'sources.json'
    config.write_text(json.dumps({'sources': [
        {'id': 'shop', 'name': 'Shop', 'path': 'SECRET_DB_PATH/business.sqlite'},
        {'id': 'archive', 'name': 'Archive', 'path': 'SECRET_DB_PATH/archive.sqlite'},
    ]}), encoding='utf-8')
    return config, {path: path.read_bytes() for path in root.glob('*.sqlite')}


def test_closed_flag_keeps_catalog_and_user_databases(tmp_path):
    config, before = source_config(tmp_path)
    registry = SourceRegistry()
    registry.load(config)
    args = live_ui.argument_parser().parse_args(['--output-dir', str(tmp_path / 'unused')])
    assert args.workspace_db is None
    app = live_ui.Observatory(None, tmp_path / 'runs', sources=config)
    assert app.catalog()['sources'] == registry.catalog()
    assert app.catalog()['databases'] == sorted(registry.databases)
    assert list(tmp_path.rglob('workspace.sqlite')) == []
    with client_for(app) as client:
        body = client.get('/api/catalog').content
        assert b'SECRET_DB_PATH' not in body
        assert b'business.sqlite' not in body
        assert b'credential-sentinel-value' not in body
        assert json.loads(body)['sources'] == registry.catalog()
        assert client.post('/api/start', json={'db_id': 'missing', 'question': 'hello'}).status_code == 400
    assert all(path.read_bytes() == content for path, content in before.items())


def test_open_flag_registers_sources_without_duplicating_or_copying_rows(tmp_path):
    config, before = source_config(tmp_path)
    registry = SourceRegistry()
    registry.load(config)
    database = tmp_path / 'meta' / 'workspace.sqlite'
    app = live_ui.Observatory(None, tmp_path / 'runs-a', sources=config, workspace_db=database)
    assert app.catalog()['sources'] == registry.catalog()
    created = {row.id: row.created_at for row in WorkspaceStore(database).list_data_sources(LOCAL_WORKSPACE_ID)}
    live_ui.Observatory(None, tmp_path / 'runs-b', sources=config, workspace_db=database)

    store = WorkspaceStore(database)
    rows = store.list_data_sources(LOCAL_WORKSPACE_ID)
    assert [(row.id, row.name, row.kind, row.read_only, row.status, row.secret_ref) for row in rows] == [
        ('archive', 'Archive', 'sqlite', True, 'ready', 'sources:archive'),
        ('shop', 'Shop', 'sqlite', True, 'ready', 'sources:shop')]
    assert {row.id: row.created_at for row in rows} == created
    assert [row.catalog() for row in rows] == registry.catalog()
    with pytest.raises(ValueError, match='not registered'):
        store.get_data_source('missing')
    blob = database.read_bytes()
    assert b'SECRET_DB_PATH' not in blob
    assert b'business.sqlite' not in blob
    assert b'credential-sentinel-value' not in blob
    with client_for(app) as client:
        body = client.get('/api/catalog').content
        catalog = json.loads(body)
        assert catalog['sources'] == registry.catalog()
        assert all('path' not in source for source in catalog['sources'])
        assert b'SECRET_DB_PATH' not in body
        assert b'credential-sentinel-value' not in body
        assert client.post('/api/start', json={'db_id': 'missing', 'question': 'hello'}).status_code == 400
    assert all(path.read_bytes() == content for path, content in before.items())
    with pytest.raises(ValueError, match='not an application workspace'):
        live_ui.Observatory(None, tmp_path / 'runs-c', sources=config, workspace_db=tmp_path / 'SECRET_DB_PATH' / 'business.sqlite')
    assert all(path.read_bytes() == content for path, content in before.items())
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM data_source').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM workspace').fetchone()[0] == 1
