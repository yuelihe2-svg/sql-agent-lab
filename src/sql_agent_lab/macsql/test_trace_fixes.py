"""Offline trace/context/evaluator checks. No model requests or benchmark reruns."""
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from .dataset import Question, Reference
from .evaluation_audit import EvaluationSession, summarize
from .fetch_bird import sha256
from .review_context import select_review_schema
from .reference_baseline import ReferenceMacSQLAgent, schema_text
from .reference_core import WorkflowConfig, ROLES
from .semantic_guard import evidence_context
from .tools import SQLiteTools, ToolLimits
from .trace_support import TraceEmitter
from .context import ContextPolicy


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket,'connect',lambda *a,**k:pytest.fail('Network forbidden in offline regression'))


@pytest.fixture
def db(tmp_path):
    p=tmp_path/'db.sqlite'
    with sqlite3.connect(p) as c:
        c.executescript("CREATE TABLE t(id TEXT,name TEXT,n INTEGER); INSERT INTO t VALUES('001','Alpha',8),('002','Beta',9);")
    metadata={'db':{'t':{'id':{'description':'Text identifier, keep leading zeroes','source':'fixture-doc'},
                         'name':{'description':'Human-readable item name','source':'fixture-doc'},
                         'n':{'enum_meanings':{'8':'medium','9':'high'},'source':'fixture-doc'}}}}
    return SQLiteTools({'db':p},metadata=metadata)


def test_metadata_only_enrichment_never_samples_rows(db,monkeypatch):
    from contextlib import contextmanager
    connect=db.connection; statements=[]
    @contextmanager
    def observed(*a,**k):
        with connect(*a,**k) as c:
            c.set_trace_callback(statements.append)
            yield c
    monkeypatch.setattr(db,'connection',observed)
    schema=db.load_schema('db',enrich=True,sample_values=False)
    assert schema['t']['columns']['name']['description']=='Human-readable item name'
    assert schema['t']['columns']['n']['enum_meanings']=={'8':'medium','9':'high'}
    assert all('observed_samples' not in c for c in schema['t']['columns'].values())
    assert not any('substr(' in s for s in statements)
    _,_,catalog=evidence_context(Question('q','db','Return names.'),schema,'SELECT name,n FROM t')
    assert {c['kind'] for c in catalog}=={'description','enum_meanings'}


def schema_fixture():
    return {'t':{'columns':{'id':{'type':'TEXT','pk':True},'name':{'type':'TEXT'},'n':{'type':'INTEGER'}},'foreign_keys':[{'column':'id','table':'parent','target':'id'}]},
            'parent':{'columns':{'id':{'type':'TEXT','pk':True}},'foreign_keys':[]},
            'unrelated':{'columns':{f'c{i}':{'type':'INTEGER'} for i in range(400)},'foreign_keys':[{'column':'c1','table':'t','target':'id'}]}}


@pytest.mark.parametrize('sql',[
    'SELECT name FROM t',
    'WITH unrelated AS (SELECT name FROM t) SELECT name FROM unrelated',
    "SELECT name FROM t WHERE id IN (SELECT id FROM parent) /* unrelated */",
    'SELECT name FROM "t" AS a'])
def test_schema_scope_keeps_physical_sources_and_fk_targets_without_reverse_fanout(sql):
    schema=schema_fixture();before=copy.deepcopy(schema)
    selected,scope=select_review_schema(schema,sql)
    assert set(selected)=={'t','parent'} and scope['omitted_tables']==['unrelated']
    assert selected['t']['columns']==schema['t']['columns'] and schema==before
    rendered=schema_text(selected,3,preserve_types=True)
    assert 'name | TEXT' in rendered and 'id | TEXT | PRIMARY KEY' in rendered
    assert 'n | INTEGER' in rendered


def test_unresolvable_sql_does_not_silently_remove_schema():
    schema=schema_fixture()
    selected,scope=select_review_schema(schema,'SELECT x FROM unknown_table')
    assert selected==schema and scope['schema_complete']
    assert scope['reason'].startswith('full_schema_fallback')


def test_review_fit_does_not_trade_types_for_budget():
    agent=ReferenceMacSQLAgent({r:'offline' for r in ROLES},config=WorkflowConfig(
        None,lambda **k:pytest.fail('No model calls'),context=ContextPolicy(input_budget=4000,context_window=8000)))
    selected,_=select_review_schema(schema_fixture(),'SELECT name FROM t')
    messages,budget=agent._fit('semantic_review',lambda s:s,selected)
    assert 'name | TEXT' in messages[-1]['content'] and budget['schema_types_preserved']


def test_session_reuses_same_sql_and_gold_and_keeps_evidence_out_of_trace(db,tmp_path,monkeypatch):
    original=db.full_result_details;calls=[];events=[]
    def counted(database,sql):calls.append(sql);return original(database,sql)
    monkeypatch.setattr(db,'full_result_details',counted)
    reference=Reference('db','SELECT name FROM t ORDER BY id')
    evaluator=EvaluationSession(db,reference,sha256(db.databases['db']),artifact=tmp_path/'evaluation-evidence.json',emit=events.append)
    first=evaluator.score('SELECT name FROM t','baseline')
    second=evaluator.score('SELECT name FROM t','submitted')
    assert first['score']==second['score']==1 and len(calls)==2
    assert second['reference_reused'] and second['candidate_reused']
    assert second['reference']['row_count']==2 and second['candidate']['column_count']==1
    assert second['reference']['result_sha256']==second['candidate']['result_sha256']
    artifact=json.loads((tmp_path/'evaluation-evidence.json').read_text())
    assert artifact['model_input'] is False
    assert 'Alpha' not in json.dumps(events) and 'SELECT name' not in json.dumps(events)
    evaluator.score('SELECT name,id FROM t','suggestion')
    assert len(calls)==3
    extra=evaluator.comparisons[-1]
    assert extra['score']==0 and extra['candidate']['column_count']==2
    assert extra['extra_rows']==extra['missing_rows']==2
    assert '001' in json.dumps(extra['extra_examples'])


@pytest.mark.parametrize('sql,status,score,reason',[
    ('SELECT missing FROM t','scored',0,'candidate_sql_error'),
    ('SELECT name FROM t WHERE n>100','scored',0,'result_mismatch')])
def test_wrong_query_and_mismatch_have_distinct_evidence(db,sql,status,score,reason):
    r=EvaluationSession(db,Reference('db','SELECT name FROM t'),'fixture').score(sql,'submitted')
    assert (r['status'],r['score'],r['reason'])==(status,score,reason)


def test_limits_and_reference_failures_never_become_zero(db):
    db.limits=ToolLimits(max_result_rows=1)
    result=EvaluationSession(db,Reference('db','SELECT name FROM t'),'fixture').score('SELECT name FROM t','submitted')
    assert result['status']=='error' and result['score'] is None
    result=EvaluationSession(db,Reference('db','SELECT broken FROM t'),'fixture').score('SELECT name FROM t','submitted')
    assert result['status']=='error' and result['reason']=='reference_evaluation_error'


def test_candidate_timeout_is_not_a_wrong_answer(db,monkeypatch):
    original=db.full_result_details
    def timed(db_id,sql):
        if 'slow' in sql:raise sqlite3.OperationalError('interrupted')
        return original(db_id,sql)
    monkeypatch.setattr(db,'full_result_details',timed)
    r=EvaluationSession(db,Reference('db','SELECT name FROM t'),'fixture').score('SELECT slow FROM t','submitted')
    assert r['score'] is None and r['status']=='error'


def test_cache_cannot_survive_database_change_or_cross_sessions(db):
    ref=Reference('db','SELECT name FROM t')
    session=EvaluationSession(db,ref,'fixture')
    assert session.score(ref.sql,'baseline')['score']==1
    with sqlite3.connect(db.databases['db']) as c:c.execute("INSERT INTO t VALUES('003','Gamma',10)")
    assert session.score(ref.sql,'submitted')['score'] is None
    new=EvaluationSession(db,ref,'new-fixture').score(ref.sql,'baseline')
    assert new['reference']['row_count']==3 and not new['reference_reused']


def test_set_scoring_and_fingerprints_preserve_numeric_equality_and_text_ids(db):
    def s(rows):return summarize(dict(rows=set(rows),columns=['x'],row_count=len(rows),unique_row_count=len(set(rows)),column_count=1))
    assert s([(1,)])['result_sha256']==s([(1.0,)])['result_sha256']
    assert s([('001',)])['result_sha256']!=s([(1,)])['result_sha256']
    r=EvaluationSession(db,Reference('db','SELECT 1 UNION ALL SELECT 1'),'fixture').score('SELECT 1.0','submitted')
    assert r['score']==1 and r['reference']['row_count']==2 and r['reference']['unique_row_count']==1


def test_trace_identity_and_clock_cover_tools_and_evaluator():
    events=[];emit=TraceEmitter(events.append,'run-1','q-1')
    for kind in ('tool_started','tool_completed','evaluation_case_completed'):emit({'event':kind})
    assert [e['event_sequence'] for e in events]==[1,2,3]
    assert all(e['run_id']=='run-1' and e['question_id']=='q-1' and e['timestamp'] for e in events)
    assert events[-1]['elapsed']>=events[0]['elapsed']


def test_review_runner_uses_enrichment_and_one_evaluation_session(db,tmp_path,monkeypatch):
    from . import run_deepseek_review as runner
    from dataclasses import asdict
    baseline=tmp_path/'baseline';baseline.mkdir();data=tmp_path/'data';data.mkdir()
    q=Question('test','db','Return names.');sql='SELECT name FROM t'
    (baseline/'manifest.json').write_text(json.dumps({'question':asdict(q)}))
    (baseline/'trace.jsonl').write_text(json.dumps({'event':'semantic_review_started','sql':sql}))
    (data/'mini_dev_sqlite.jsonl').write_text(json.dumps({'question_id':'test','db_id':'db','question':q.question,'SQL':sql}))
    (data/'source-lock.json').write_text(json.dumps({'annotations_sha256':sha256(data/'mini_dev_sqlite.jsonl'),
        'databases':{'db':{'path':str(db.databases['db']),'sha256':sha256(db.databases['db'])}}}))
    metadata=tmp_path/'metadata.json';metadata.write_text(json.dumps({'databases':db.metadata}))
    out=tmp_path/'result';captured={}
    class OfflineAgent:
        def __init__(self,models,config):pass
        def _check_output(self,*a):return {'status':'ok'}
        def _semantic_review(self,state):
            captured.update(state)
            assert 'evaluation' not in state and 'reference' not in state
            return {'semantic_status':'accepted','semantic_metrics':{}}
    monkeypatch.setattr(runner,'ReferenceMacSQLAgent',OfflineAgent)
    monkeypatch.setattr(runner,'DeepSeekModel',lambda *a,**k:None)
    monkeypatch.setenv('DEEPSEEK_API_KEY','offline-test-placeholder')
    monkeypatch.setattr('sys.argv',['test','--baseline',str(baseline),'--data',str(data),'--metadata',str(metadata),'--output',str(out),'--execute','--suggestions-only'])
    runner.main()
    assert captured['full_schema']['t']['columns']['name']['documentation_source']=='fixture-doc'
    result=json.loads((out/'result.json').read_text())
    assert result['score']==result['baseline_score']==1 and result['cloud_api_calls']==0
    evidence=json.loads((out/'evaluation-evidence.json').read_text())
    assert evidence['comparisons'][1]['candidate_reused']
    events=[json.loads(s) for s in (out/'trace.jsonl').read_text().splitlines()]
    assert all('run_id' in e and 'question_id' in e for e in events)
    assert not (out/'api-calls.jsonl').exists()
