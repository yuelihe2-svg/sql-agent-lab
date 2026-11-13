"""Saved real trace regressions; no provider requests or benchmark execution."""
import json
from pathlib import Path
import sqlite3

import pytest

from .dataset import Question
from .context import ContextPolicy, ContextBudgetExceeded
from .generation_budget import generation_budget
from .schema_recovery import recovery_schema
from .trace_support import TraceEmitter, review_timing, event_category
from .reference_baseline import ReferenceMacSQLAgent
from .reference_core import WorkflowConfig, ModelReply, ROLES
from .tools import SQLiteTools

CASES = json.loads((Path(__file__).parent / 'fixtures/trace_budget_schema_20261111.json').read_text(encoding='utf8'))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('No model/network calls allowed'))


@pytest.mark.parametrize('qid', ['40', '174', '184', '230', '234'])
def test_truncated_real_cases_receive_more_room_without_retry(qid):
    case = CASES[qid]
    decision = generation_budget(Question(**case['question']))
    assert decision['max_output_tokens'] == 2400
    assert decision['automatic_retry'] is False
    assert case['failure']['code'] == 'output_truncated'
    assert event_category(case['failure']) == 'model_output_truncated'


def test_simple_question_keeps_small_output():
    assert generation_budget(Question('synthetic', 'db', 'Return the names.'))['max_output_tokens'] == 1000


def test_real_cte_missing_column_restores_physical_owner():
    case = CASES['222']; full = case['schema']; sql = case['validation']['sql']
    selected = {'district': {**full['district'], 'columns': {'district_id': full['district']['columns']['district_id']}}}
    schema, report = recovery_schema(full, selected, sql, case['validation']['feedback'])
    assert 'A2' in schema['district']['columns']
    assert report['identifier_found_in_database'] and 'district' in report['exact_match_tables']
    assert 'CTE/subquery' in report['note']


def test_real_extraction_failure_time_is_not_sql_review_time():
    case = CASES['472']; original = json.dumps(case, sort_keys=True)
    timing = review_timing(case['result'], case['events'])
    assert timing['requirements_seconds'] == case['result']['semantic_metrics']['elapsed_seconds']
    assert timing['sql_review_seconds'] is None and timing['sql_review_ran'] is False
    assert timing['total_review_seconds'] == timing['requirements_seconds']
    assert timing['run_seconds'] == case['result']['elapsed_seconds']
    assert json.dumps(case, sort_keys=True) == original


def test_unknown_historical_time_is_not_inferred():
    timing = review_timing({'semantic_metrics': {'elapsed_seconds': 9}, 'elapsed_seconds': 100})
    assert timing['sql_review_seconds'] is None and timing['requirements_seconds'] is None
    assert timing['run_seconds'] == 100


@pytest.fixture
def tools(tmp_path):
    p = tmp_path/'db.sqlite'
    with sqlite3.connect(p) as conn:
        conn.executescript("CREATE TABLE t(id INTEGER, population TEXT); INSERT INTO t VALUES(1,'300');")
    return SQLiteTools({'db': p})


def test_missing_column_graph_refreshes_once_without_selector_call(tools, monkeypatch):
    calls, events, reads = [], [], []
    original = tools.load_schema
    def load(*a, **k):
        reads.append(k)
        return original(*a, **k)
    monkeypatch.setattr(tools, 'load_schema', load)
    def model(**kwargs):
        calls.append(kwargs['role'])
        if kwargs['role'] == 'decomposer':
            sql = 'WITH c AS (SELECT id FROM t) SELECT population FROM c'
        else:
            assert 'population' in kwargs['messages'][-1]['content']
            sql = 'WITH c AS (SELECT id, population FROM t) SELECT CAST(population AS INTEGER) FROM c'
        return ModelReply('```sql\n'+sql+'\n```', 0, 0, simulated=True)
    agent = ReferenceMacSQLAgent({r:'offline' for r in ROLES}, config=WorkflowConfig(tools, model, trace=events.append))
    sql = agent.run(Question('synthetic','db','Return the population.'))
    assert calls == ['decomposer', 'refiner']
    refresh = [e for e in events if e['event']=='schema_refreshed']
    assert len(refresh)==1 and refresh[0]['model_call'] is False
    assert sum(k.get('sample_values') is False for k in reads)==1
    assert events[-1]['schema_refresh_count']==1
    assert tools.execute_sql_readonly('db',sql)['preview_rows']==[[300]]


def test_missing_table_and_invented_column_are_not_fabricated(tools):
    full = tools.load_schema('db')
    schema, report = recovery_schema(full, {}, 'SELECT * FROM t', {'identifier_kind':'missing_table','identifier':'t'})
    assert schema == full and report['identifier_found_in_database']
    schema, report = recovery_schema(full, full, 'SELECT fictional FROM t', {'identifier_kind':'missing_column','identifier':'fictional'})
    assert not report['identifier_found_in_database']
    assert 'fictional' not in schema['t']['columns']


def test_recovery_preserves_ambiguous_owners_and_quoted_dotted_names():
    full={t:{'columns':{'measure.value':{'type':'REAL'}},'foreign_keys':[]} for t in ['first','second']}
    schema,report=recovery_schema(full,{},'SELECT 1',{'identifier_kind':'missing_column','identifier':'measure.value'})
    assert schema==full and report['exact_match_tables']==['first','second']


def test_second_repair_does_not_reload_schema(tools, monkeypatch):
    full=tools.load_schema('db')
    monkeypatch.setattr(tools,'load_schema',lambda *a,**k:pytest.fail('Second refresh forbidden'))
    agent=ReferenceMacSQLAgent({r:'offline' for r in ROLES},config=WorkflowConfig(tools,
        lambda **k:ModelReply('```sql\nSELECT id FROM t\n```',0,0,simulated=True)))
    state={'question':Question('q','db','Return IDs'),'sql':'SELECT absent FROM t','selected_schema':full,'full_schema':full,
           'schema_refresh_count':1,'repair_count':1,'feedback':{'status':'error','identifier_kind':'missing_column','identifier':'absent'}}
    assert agent._refine(state)['schema_refresh_count']==1


def test_larger_output_still_respects_context_limit(tools):
    agent=ReferenceMacSQLAgent({r:'offline' for r in ROLES},config=WorkflowConfig(tools,None,
        context=ContextPolicy(input_budget=2400,context_window=4096)))
    _, budget=agent._fit('decomposer',lambda s:'short',{},output_limit=2400)
    assert budget['input_budget_limit']==4096-2400-512
    with pytest.raises(ContextBudgetExceeded):
        agent._fit('decomposer',lambda s:'x'*2000,{},output_limit=2400)


def test_trace_categories_separate_warnings_and_failure():
    events=[];emit=TraceEmitter(events.append,'test','q')
    for e in [CASES['40']['failure'],{'event':'output_type_checked','report':{'status':'unverified'}},
              {'event':'workflow_completed','semantic_status':'disabled'},
              {'event':'execution','feedback':{'status':'error','error_type':'OperationalError','message':'interrupted'}}]:emit(e)
    assert [e['diagnostic_category'] for e in events]==['model_output_truncated','output_type_warning','semantic_not_run','execution_timeout']
