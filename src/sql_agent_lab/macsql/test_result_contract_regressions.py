"""Real recorded replies plus small adversarial SQLite fixtures; no model network."""
import copy
import json
from pathlib import Path
import sqlite3

import pytest

from .output_types import check_output_types
from .result_risks import inspect_result_risks
from .tools import SQLiteTools, ToolLimits
from .test_execution_repair_only import run


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('No network permitted'))


def cases():
    return json.loads((Path(__file__).parent / 'fixtures/result_contract_regressions_20261111.json').read_text(encoding='utf8'))


def test_real_wide_query_checks_sixteenth_column_and_old_scope():
    case = cases()[0]
    old = check_output_types(case['sql'], case['feedback'], case['schema'])
    assert old['column_scope'] == 'partial_columns' and old['scope'] != 'complete'
    new = check_output_types(case['sql'], case['full_feedback'], case['schema'])
    assert new['checked_columns'] == 17 and new['column_scope'] == 'all_columns'
    assert [(c['index'], c['expected']) for c in new['columns'] if c['status'] == 'mismatch'] == [(15, 'integer')]


@pytest.mark.parametrize('position', [0, 12, 15, 24])
def test_profile_is_independent_of_preview_width(tmp_path, position):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE t(population TEXT, postal_code TEXT); INSERT INTO t VALUES('123','00123');")
    tools = SQLiteTools({'db': path}, ToolLimits(preview_columns=2))
    columns = ['postal_code'] * 25
    columns[position] = 'population'
    sql = 'SELECT ' + ','.join(columns) + ' FROM t'
    feedback = tools.execute_sql_readonly('db', sql)
    assert len(feedback['columns']) == len(feedback['preview_rows'][0]) == 2
    assert feedback['column_count'] == len(feedback['output_type_profile']['columns']) == 25
    report = check_output_types(sql, feedback, tools.load_schema('db'))
    assert [c['index'] for c in report['columns'] if c['status'] == 'mismatch'] == [position]
    assert all(c['expected'] == 'text' for c in report['columns'] if c['index'] != position)


def test_wide_repair_gate_rejects_width_change_beyond_preview(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE t(population TEXT, postal_code TEXT); INSERT INTO t VALUES('123','00123');")
    tools = SQLiteTools({'db': path})
    initial = 'SELECT ' + ','.join(['postal_code'] * 15 + ['population']) + ' FROM t'
    fixed = 'SELECT ' + ','.join(['postal_code'] * 15 + ['CAST(population AS INTEGER)']) + ' FROM t'
    bad = fixed.replace(' FROM t', ', postal_code FROM t')
    answer, calls, events = run(tools, initial, bad)
    assert answer == initial and calls == ['decomposer', 'refiner']
    assert next(e for e in events if e['event'] == 'repair_candidate_checked')['reason'] == 'output_shape_changed'
    answer, calls, events = run(tools, initial, fixed)
    assert answer == fixed and calls == ['decomposer', 'refiner']


def test_sample_rows_stay_bounded_but_columns_do_not(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE t(id INTEGER); INSERT INTO t VALUES(1),(2),(3);')
    tools = SQLiteTools({'db': path}, ToolLimits(preview_columns=1, type_sample_rows=2))
    sql = 'SELECT id, id FROM t'
    report = check_output_types(sql, tools.execute_sql_readonly('db', sql), tools.load_schema('db'))
    assert report['sampled_rows'] == 2 and report['row_scope'] == 'bounded_sample'
    assert report['column_scope'] == 'all_columns' and report['scope'] == 'bounded_sample'


@pytest.mark.parametrize('index,code', [(1, 'grouped_count_inner_join'), (2, 'null_identifier_output')])
def test_real_semantic_failures_are_advisories_not_automatic_rewrites(index, code):
    c = cases()[index]
    saved = copy.deepcopy(c)
    types = check_output_types(c['sql'], c['full_feedback'], c['schema'])
    report = inspect_result_risks(c['sql'], c['full_feedback'], types)
    assert code in [f['code'] for f in report['findings']]
    assert not report['automatic_rewrite'] and not report['triggers_model_call']
    assert c == saved


@pytest.fixture
def entity_tools(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript("""
          CREATE TABLE people(UserId TEXT PRIMARY KEY, nickname TEXT);
          CREATE TABLE activities(id INTEGER, UserId TEXT, accepted INTEGER);
          INSERT INTO people VALUES('001',NULL),('002','Bee'),('003','Cee');
          INSERT INTO activities VALUES(1,'001',1),(2,'001',0),(3,'003',0),(4,NULL,1);
        """)
    return SQLiteTools({'db': path})


def test_zero_count_semantics_preserve_entities_and_filter_counted_relation(entity_tools):
    inner = 'SELECT p.UserId, COUNT(a.id) FROM people p JOIN activities a ON a.UserId=p.UserId AND a.accepted=1 GROUP BY p.UserId'
    outer = inner.replace(' JOIN activities', ' LEFT JOIN activities')
    assert entity_tools.full_result_set('db', inner) == {('001', 1)}
    assert entity_tools.full_result_set('db', outer) == {('001', 1), ('002', 0), ('003', 0)}
    # Positive-count questions must NOT acquire zero rows.
    positive = inner + ' HAVING COUNT(a.id)>0'
    answer, calls, _ = run(entity_tools, positive)
    assert answer == positive and calls == ['decomposer']


@pytest.mark.parametrize('sql,expected', [
    ('SELECT UserId FROM activities WHERE UserId IS NULL', {(None,)}),
    ('SELECT UserId, nickname FROM people', {('001', None), ('002', 'Bee'), ('003', 'Cee')}),
    ('SELECT DISTINCT UserId FROM activities', {('001',), ('003',), (None,)}),
    ('SELECT DISTINCT UserId FROM activities WHERE UserId IS NOT NULL', {('001',), ('003',)}),
])
def test_null_intent_is_not_guessed_and_text_ids_stay_text(entity_tools, sql, expected):
    answer, calls, events = run(entity_tools, sql)
    assert answer == sql and calls == ['decomposer']
    assert entity_tools.full_result_set('db', answer) == expected
    assert any(e['event'] == 'result_risks_checked' for e in events)


def test_camel_case_identifiers_survive_sql_normalization(entity_tools):
    sql = 'SELECT UserId FROM activities'
    report = check_output_types(sql, entity_tools.execute_sql_readonly('db', sql), entity_tools.load_schema('db'))
    assert report['columns'][0]['protected_identifier']
    assert report['columns'][0]['expected'] == 'text'
    risks = inspect_result_risks(sql, entity_tools.execute_sql_readonly('db', sql), report)
    assert risks['findings'][0]['code'] == 'null_identifier_output'


def test_recorded_bond_query_reproduces_zero_count_loss(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript("""
          CREATE TABLE atom(atom_id TEXT, molecule_id TEXT, element TEXT);
          CREATE TABLE molecule(molecule_id TEXT, label TEXT);
          CREATE TABLE connected(atom_id TEXT, atom_id2 TEXT, bond_id TEXT);
          INSERT INTO molecule VALUES('zero','+'),('linked','+'),('common','+'),('other','-');
          INSERT INTO atom VALUES('a','zero','rare1'),('b','linked','rare2'),
             ('c','common','shared'),('d','other','shared');
          INSERT INTO connected VALUES('b','x','b1'),('b','y','b1'),('c','z','b2');
        """)
    tools = SQLiteTools({'db': path})
    sql = cases()[1]['sql']
    assert tools.full_result_set('db', sql) == {('linked', 1)}
    # An offline diagnostic example, never an automatic rewrite rule.
    candidate = sql.replace('JOIN connected AS c', 'LEFT JOIN connected AS c')
    assert tools.full_result_set('db', candidate) == {('linked', 1), ('zero', 0)}


def test_recorded_user_query_reproduces_null_extra_row(tmp_path):
    path = tmp_path / 'db.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript("""
          CREATE TABLE postHistory(UserId INTEGER, PostId INTEGER);
          CREATE TABLE posts(Id INTEGER, ViewCount INTEGER);
          INSERT INTO posts VALUES(1,1000),(2,1000),(3,999);
          INSERT INTO postHistory VALUES(NULL,1),(7,1),(7,2),(8,2),(8,2),(9,3);
        """)
    tools = SQLiteTools({'db': path})
    sql = cases()[2]['sql']
    assert tools.full_result_set('db', sql) == {(None,), (7,)}
    candidate = sql.replace('WHERE p.ViewCount >= 1000', 'WHERE p.ViewCount >= 1000 AND ph.UserId IS NOT NULL')
    assert tools.full_result_set('db', candidate) == {(7,)}
