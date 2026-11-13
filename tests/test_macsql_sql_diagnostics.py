import hashlib
import json
import sqlite3
import pytest
from sql_agent_lab.macsql.tools import SQLiteTools
from sql_agent_lab.macsql.reference_core import WorkflowConfig, ModelReply, ROLES
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent
from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql.review_contract import CHECKS
from sql_agent_lab.macsql.live_ui import ObservedTools
from threading import Event


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'diagnostics.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript("CREATE TABLE books(title TEXT, publisher TEXT, genre TEXT);"
                       "INSERT INTO books VALUES('A','Cedar Press','History'),('B','Other','History');"
                       "CREATE TABLE borrowed(title TEXT); INSERT INTO borrowed VALUES('A'),(NULL);")
    return SQLiteTools({'library': path}), path


@pytest.mark.parametrize('clause,status', [('', 'observed_null'), (' WHERE title IS NOT NULL', 'no_null_found')])
def test_probe_checks_actual_subquery_with_its_filter(db, clause, status):
    tools, path = db; before = hashlib.sha256(path.read_bytes()).hexdigest()
    sql = f'SELECT title FROM books WHERE title NOT IN (SELECT title FROM borrowed{clause})'
    r = tools.diagnose_sql_readonly('library', sql)
    assert r['findings'][0]['status'] == status
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_correlated_probe_is_unknown_not_absence(db):
    tools, _ = db
    sql = 'SELECT b.title FROM books b WHERE b.title NOT IN (SELECT x.title FROM borrowed x WHERE x.title=b.title)'
    r = tools.diagnose_sql_readonly('library', sql)
    assert r['findings'][0]['status'] == 'unknown'
    assert 'probe_error' in r['findings'][0]


def test_probe_timeout_is_bounded_and_not_a_clean_bill_of_health(db):
    tools, _ = db
    sql = ('SELECT title FROM books WHERE title NOT IN '
           '(WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq) SELECT x AS value FROM seq)')
    r = tools.diagnose_sql_readonly('library', sql, timeout_seconds=0.01)
    assert r['elapsed_seconds'] < 1
    assert r['findings'][0]['status'] == 'unknown'


def test_not_in_text_literals_are_not_executed_as_queries(db):
    tools, _ = db
    assert tools.diagnose_sql_readonly('library', "SELECT 'NOT IN (SELECT title FROM borrowed)' FROM books")['findings'] == []


def test_multiple_statements_and_writes_are_rejected(db):
    tools, path = db; before = path.read_bytes()
    for sql in ['DELETE FROM books', 'SELECT 1; DELETE FROM books']:
        assert tools.diagnose_sql_readonly('library', sql)['status'] == 'unverified'
    assert path.read_bytes() == before


def test_probes_are_visible_as_tool_events(db):
    tools, _ = db; events = []
    observed = ObservedTools(tools, events.append, Event())
    observed.diagnose_sql_readonly('library', 'SELECT title FROM books')
    assert [e['event'] for e in events] == ['tool_started', 'tool_completed']
    assert events[-1]['tool'] == 'diagnose_sql_readonly'


def title_contract(q, report):
    requested=[{'id':'o1','source':'question','quote':'titles','description':'book titles',
                'kind':'attribute','support':'explicit','representation':'not_applicable'}]
    report['output_contract']={'requested':requested,'columns':[{'position':1,'expression':'title',
        'disposition':'required','requested':['o1'],'reason':'explicit titles'}]}
    return {'status':'clear','requested':requested,'constraints':[],'ambiguities':[]}


def test_grounded_null_revision_changes_real_rows_and_keeps_readonly_db(db):
    tools, path = db; before_hash = hashlib.sha256(path.read_bytes()).hexdigest(); events = []
    before = 'SELECT title FROM books WHERE title NOT IN (SELECT title FROM borrowed)'
    after = 'SELECT title FROM books WHERE NOT EXISTS (SELECT 1 FROM borrowed b WHERE b.title=books.title)'
    q = Question('unseen', 'library', 'Return titles of books not borrowed.')
    checks = {k: {'status': 'pass', 'reason': 'not required'} for k in CHECKS}
    checks['filters'] = {'status': 'fail', 'reason': 'NULL prevents nonmatching books from passing'}
    report = {'action': 'revise', 'checks': checks, 'requirements': [
        {'id': 'r1', 'quote': 'Return titles of books', 'check': 'output', 'status': 'pass',
         'sql_fragment': 'SELECT title FROM books', 'reason': 'requested output'},
        {'id': 'r2', 'quote': 'not borrowed.', 'check': 'filters', 'status': 'fail',
         'sql_fragment': 'title NOT IN (SELECT title FROM borrowed)', 'reason': 'NULL risk'}],
        'changes': [{'requirements': ['r2'], 'checks': ['filters'],
                     'before': 'title NOT IN (SELECT title FROM borrowed)',
                     'after': 'NOT EXISTS (SELECT 1 FROM borrowed b WHERE b.title=books.title)',
                     'requirement_quote': 'not borrowed',
                     'evidence': [{'source': 'question', 'quote': 'not borrowed'}]}]}
    calls = []
    outputs = title_contract(q,report)
    def model(**kw):
        calls.append(kw)
        if kw['messages'][-1]['content'].startswith('OUTPUT_REQUIREMENTS_V3'):
            return ModelReply(json.dumps(outputs),1,1,simulated=True)
        assert 'observed_null' in kw['messages'][-1]['content']
        return ModelReply(json.dumps(report), 12, 34, simulated=True)
    agent = ReferenceMacSQLAgent({r: 'test' for r in ROLES}, config=WorkflowConfig(tools, model, trace=events.append, semantic_review=True))
    result = agent._semantic_review({'run_id': 'test', 'question': q, 'sql': before,
                                    'full_schema': tools.load_schema('library'),
                                    'feedback': tools.execute_sql_readonly('library', before)})
    assert result['sql'] == after
    assert result['feedback']['preview_rows'] == [['B']]
    assert result['semantic_metrics']['outcome'] == 'revision_applied_unverified'
    assert len(calls) == 2 and all(c['role']=='refiner' for c in calls)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash


def test_accept_with_observed_null_remains_visibly_warned(db):
    tools, _ = db
    sql = 'SELECT title FROM books WHERE title NOT IN (SELECT title FROM borrowed)'
    q = Question('unseen2', 'library', 'Return titles not borrowed.')
    report = {'action': 'accept', 'checks': {k: {'status': 'pass', 'reason': 'matches'} for k in CHECKS},
              'requirements': [{'id': 'r1', 'quote': q.question, 'check': 'filters', 'status': 'pass',
                                'sql_fragment': sql, 'reason': 'model claims this is intentional'}]}
    outputs = title_contract(q,report)
    def model(**kw):
        extraction=kw['messages'][-1]['content'].startswith('OUTPUT_REQUIREMENTS_V3')
        return ModelReply(json.dumps(outputs if extraction else report),1,1,simulated=True)
    agent = ReferenceMacSQLAgent({r: 'test' for r in ROLES}, config=WorkflowConfig(
        tools, model, semantic_review=True))
    result = agent._semantic_review({'run_id': 'test', 'question': q, 'sql': sql,
                                    'full_schema': tools.load_schema('library'),
                                    'feedback': tools.execute_sql_readonly('library', sql)})
    assert result['semantic_status'] == 'accepted_with_warnings'
    assert 'sql' not in result
