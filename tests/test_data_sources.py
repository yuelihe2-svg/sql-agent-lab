"""Real local HTTP and SQLite; all model construction/execution is replaced."""
from contextlib import closing, contextmanager
from dataclasses import asdict
from http.server import ThreadingHTTPServer
import json
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

from sql_agent_lab.macsql import live_ui
from sql_agent_lab.macsql.data_sources import SourceRegistry
from sql_agent_lab.macsql.fetch_bird import sha256


def database(path, value):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE numbers(n INTEGER)')
        conn.execute('INSERT INTO numbers VALUES (?)', (value,))
        conn.commit()
    return path


def config_file(tmp_path, entries):
    config = tmp_path / 'sources.json'
    config.write_text(json.dumps({'sources': entries}), encoding='utf-8')
    return config


@pytest.fixture
def sources(tmp_path):
    database(tmp_path / 'first.sqlite', 42)
    database(tmp_path / 'second.sqlite', 99)
    return config_file(tmp_path, [
        {'id': 'first', 'name': '第一份数据', 'path': 'first.sqlite'},
        {'id': 'second', 'path': 'second.sqlite'}])


def bird_fixture(tmp_path):
    directory = tmp_path / 'bird'
    directory.mkdir()
    db = database(directory / 'bird.sqlite', 7)
    annotations = directory / 'mini_dev_sqlite.jsonl'
    annotations.write_text(json.dumps({'question_id': 1, 'db_id': 'bird',
        'question': 'Return n', 'SQL': 'SELECT n FROM numbers'}) + '\n')
    (directory / 'source-lock.json').write_text(json.dumps({
        'annotations_sha256': sha256(annotations),
        'databases': {'bird': {'path': 'bird.sqlite', 'sha256': sha256(db)}}}))
    return directory


def mock_agent(app, monkeypatch):
    seen = []
    monkeypatch.setattr(live_ui, 'LocalOllamaModel', lambda *a, **kw: SimpleNamespace(metadata={}))

    class FakeAgent:
        def __init__(self, models, *, config):
            self.config = config

        def run(self, question):
            seen.append(asdict(question))
            assert set(asdict(question)) == {'question_id', 'db_id', 'question', 'evidence'}
            self.config.tools.load_schema(question.db_id)
            return 'SELECT n FROM numbers'

    monkeypatch.setattr(live_ui, 'ReferenceMacSQLAgent', FakeAgent)
    original = app.work
    done = threading.Event()

    def work(*args):
        try:
            original(*args)
        finally:
            done.set()

    monkeypatch.setattr(app, 'work', work)
    return done, seen


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


def test_standalone_http_two_sources_without_bird(tmp_path, sources, monkeypatch):
    before = {p: p.read_bytes() for p in tmp_path.glob('*.sqlite')}
    app = live_ui.Observatory(None, tmp_path / 'runs', sources=sources)
    done, seen = mock_agent(app, monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail('Ordinary questions must not instantiate an evaluator')

    monkeypatch.setattr(live_ui, 'EvaluationSession', forbidden)
    with client_for(app) as client:
        catalog = client.get('/api/catalog').json()
        assert catalog['benchmark_available'] is False
        assert catalog['questions'] == []
        assert catalog['databases'] == ['first', 'second']
        assert catalog['sources'][0] == {'id': 'first', 'name': '第一份数据', 'kind': 'sqlite', 'read_only': True}
        assert all('path' not in s for s in catalog['sources'])
        for db_id, value in [('first', 42), ('second', 99)]:
            done.clear()
            response = client.post('/api/start', json={'db_id': db_id, 'question': 'Return n'})
            assert response.status_code == 200
            assert done.wait(5)
            run_id = response.json()['id']
            run = client.get('/api/run', params={'id': run_id}).json()
            assert run['task']['state'] == 'completed'
            assert run['manifest']['benchmark'] is False
            assert run['result']['score'] is None
            assert run['result']['feedback']['preview_rows'] == [[value]]
            assert not (app.output / run_id / 'evaluation-evidence.json').exists()
            assert not any(e['event'].startswith('evaluation_') for e in run['events'])
    assert [q['db_id'] for q in seen] == ['first', 'second']
    assert all(path.read_bytes() == content for path, content in before.items())
    assert live_ui.Observatory(None, app.output, sources=sources).read_run(run_id)['task']['state'] == 'completed'


@pytest.mark.parametrize('payload', [
    {'question_id': '1'}, {'db_id': 'missing', 'question': 'hello'},
    {'db_id': ['first'], 'question': 'hello'}, {'db_id': 'first', 'question': ''},
    {'path': 'first.sqlite', 'question': 'hello'}])
def test_invalid_requests_create_no_task(tmp_path, sources, payload):
    app = live_ui.Observatory(None, tmp_path / 'runs', sources=sources)
    with client_for(app) as client:
        assert client.post('/api/start', json=payload).status_code == 400
    assert app.active is None
    assert list(app.output.iterdir()) == []


def test_bird_and_independent_sources_coexist(tmp_path, sources, monkeypatch):
    bird = bird_fixture(tmp_path)
    app = live_ui.Observatory(bird, tmp_path / 'runs', sources=sources)
    done, seen = mock_agent(app, monkeypatch)
    catalog = app.catalog()
    assert catalog['benchmark_available'] is True
    assert catalog['databases'] == ['bird', 'first', 'second']
    assert len(catalog['questions']) == 1
    assert 'SQL' not in catalog['questions'][0]
    with pytest.raises(ValueError, match='selected database'):
        app.start({'question_id': '1', 'db_id': 'first'})
    run_id = app.start({'question_id': '1'})
    assert done.wait(5)
    run = app.read_run(run_id)
    assert run['result']['score'] == 1
    assert run['manifest']['benchmark'] is True
    assert (app.output / run_id / 'evaluation-evidence.json').is_file()
    assert 'sql' not in seen[0]
    # A changed benchmark database fails before the mocked Agent is entered.
    with closing(sqlite3.connect(bird / 'bird.sqlite')) as conn:
        conn.execute('INSERT INTO numbers VALUES (8)')
        conn.commit()
    done.clear()
    bad_id = app.start({'question_id': '1'})
    assert done.wait(5)
    assert app.read_run(bad_id)['task']['error'] == 'Database checksum mismatch'
    assert len(seen) == 1


@pytest.mark.parametrize('case', ['missing', 'text', 'corrupt', 'directory', 'duplicate', 'invalid_id', 'kind'])
def test_reject_bad_source_config_without_modifying_files(tmp_path, case):
    path = database(tmp_path / 'valid.sqlite', 1)
    entry = {'id': 'demo', 'path': path.name}
    if case == 'missing':
        entry['path'] = 'absent.sqlite'
    elif case == 'text':
        path.write_text('not a database')
    elif case == 'corrupt':
        path.write_bytes(b'SQLite format 3\x00' + b'broken' * 30)
    elif case == 'directory':
        entry['path'] = '.'
    elif case == 'invalid_id':
        entry['id'] = '../demo'
    elif case == 'kind':
        entry['kind'] = 'postgres'
    config = config_file(tmp_path, [entry, entry] if case == 'duplicate' else [entry])
    before = {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        SourceRegistry().load(config)
    assert {p: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


def test_duplicate_between_bird_and_user_sources_is_rejected(tmp_path, sources):
    bird = bird_fixture(tmp_path)
    document = json.loads(sources.read_text())
    document['sources'][0]['id'] = 'bird'
    sources.write_text(json.dumps(document))
    with pytest.raises(ValueError, match='Duplicate data source ID'):
        live_ui.Observatory(bird, tmp_path / 'runs', sources=sources)


def test_missing_sources_fails_clearly(tmp_path):
    with pytest.raises(ValueError, match='Configure at least one'):
        live_ui.Observatory(None, tmp_path / 'runs')
