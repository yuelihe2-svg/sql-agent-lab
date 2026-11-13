"""Offline regressions for conditional repair; sockets are forbidden."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from .dataset import Question
from .reference_core import ModelReply, WorkflowConfig, ROLES
from .reference_baseline import ReferenceMacSQLAgent
from .output_types import check_output_types
from .tools import SQLiteTools


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('No network in offline tests'))


@pytest.fixture
def tools(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE t(id TEXT PRIMARY KEY, name TEXT, population TEXT);"
                           "INSERT INTO t VALUES('001','Alpha','100'),('002','Beta','200');")
    return SQLiteTools({'db': path})


def run(tools, initial, candidate=None, **config):
    events, calls = [], []
    def model(**kwargs):
        calls.append(kwargs['role'])
        assert kwargs['role'] in ('decomposer', 'refiner')
        assert 'OUTPUT_REQUIREMENTS_' not in str(kwargs['messages'])
        assert 'SEMANTIC_REVIEW_' not in str(kwargs['messages'])
        sql = initial if kwargs['role'] == 'decomposer' else candidate
        assert sql is not None, 'Unexpected paid-equivalent model call'
        return ModelReply(f'```sql\n{sql}\n```', 0, 0, simulated=True)
    agent = ReferenceMacSQLAgent({r: 'offline' for r in ROLES},
        config=WorkflowConfig(tools, model, trace=events.append, **config))
    answer = agent.run(Question('fixture', 'db', 'Return the requested data'))
    return answer, calls, events


@pytest.mark.parametrize('sql', ['SELECT name FROM t', 'SELECT name FROM t WHERE 0', 'SELECT NULL AS x'])
def test_valid_empty_and_null_need_only_generation(tools, sql):
    answer, calls, events = run(tools, sql)
    assert answer == sql and calls == ['decomposer']
    assert events[0]['workflow_mode'] == 'execution_repair_only'
    assert events[-1]['semantic_status'] == 'disabled'


def test_execution_error_repaired_once(tools):
    answer, calls, events = run(tools, 'SELECT nonexistent FROM t', 'SELECT name FROM t')
    assert answer == 'SELECT name FROM t' and calls == ['decomposer', 'refiner']
    assert next(e for e in events if e['event'] == 'repair_candidate_checked')['adopted']


@pytest.mark.parametrize('candidate', [
    'SELECT nonexistent FROM t',
    'SELECT CAST(population AS INTEGER), id FROM t',
    'SELECT CAST(id AS INTEGER) FROM t',
    'DELETE FROM t',
])
def test_bad_candidate_preserves_executable_original(tools, candidate):
    initial = 'SELECT population FROM t'
    answer, calls, events = run(tools, initial, candidate)
    assert answer == initial and calls == ['decomposer', 'refiner']
    assert events[-1]['repair_rejected']
    assert not next(e for e in events if e['event'] == 'repair_candidate_checked')['adopted']
    assert tools.execute_sql_readonly('db', 'SELECT COUNT(*) FROM t')['preview_rows'] == [[2]]


def test_bad_original_and_bad_candidate_stop_with_failure(tools):
    initial = 'SELECT missing FROM t'
    answer, calls, events = run(tools, initial, 'SELECT also_missing FROM t', max_repairs=3)
    assert answer == initial and calls == ['decomposer', 'refiner']
    assert events[-1]['status'] == 'error'


def test_type_repair_retains_leading_zero_identifiers(tools):
    before = 'SELECT population, id FROM t'
    after = 'SELECT CAST(population AS INTEGER), id FROM t'
    answer, calls, events = run(tools, before, after)
    assert answer == after and len(calls) == 2
    assert tools.execute_sql_readonly('db', answer)['preview_rows'] == [[100, '001'], [200, '002']]


@pytest.mark.parametrize('qid', ['150', '472', '327', '203'])
def test_saved_real_trace_type_reports(qid):
    cases = json.loads((Path(__file__).parent / 'fixtures/type_regressions_20261111.json').read_text(encoding='utf8'))
    case = next(c for c in cases if c['question_id'] == qid)
    report = check_output_types(case['sql'], case['feedback'], case['schema'])
    if case['expected'] == 'mismatch':
        assert report['status'] == 'mismatch'
    else:
        assert report['status'] != 'mismatch'


def test_deepseek_worker_defaults_to_no_review_and_three_call_cap(tools, tmp_path, monkeypatch):
    from . import run_deepseek as runner
    from .fetch_bird import sha256
    data = tmp_path / 'data'; data.mkdir()
    output = tmp_path / 'results'; output.mkdir()
    import shutil
    db = data / 'db.sqlite'; shutil.copy2(tools.databases['db'], db)
    (data / 'source-lock.json').write_text(json.dumps({'databases': {'db': {'path': 'db.sqlite', 'sha256': sha256(db)}}}))
    (data / 'mini_dev_sqlite.jsonl').write_text(json.dumps({'question_id': '118', 'db_id': 'db', 'question': 'Return names', 'SQL': 'SELECT name FROM t'}))
    metadata = data / 'metadata.json'; metadata.write_text('{"databases": {}}')
    calls, caps = [], []
    original_ledger = runner.Ledger
    def ledger(*a, **k):
        caps.append(k['max_calls'])
        return original_ledger(*a, **k)
    def fake_model(*a, **k):
        def call(**kwargs):
            calls.append(kwargs['role'])
            assert kwargs['role'] == 'decomposer'
            return ModelReply('```sql\nSELECT name FROM t\n```', 0, 0, simulated=True)
        return call
    monkeypatch.setattr(runner, 'DeepSeekModel', fake_model)
    monkeypatch.setattr(runner, 'Ledger', ledger)
    runner.worker(SimpleNamespace(ids=['118'], output=output, data=data, metadata=metadata))
    result = json.loads((output / 'q118/result.json').read_text())
    manifest = json.loads((output / 'q118/manifest.json').read_text())
    assert calls == ['decomposer'] and caps == [3]
    assert result['score'] == 1 and result['semantic_status'] == 'disabled'
    assert manifest['semantic_review'] is False
    assert manifest['workflow_mode'] == 'execution_repair_only'
