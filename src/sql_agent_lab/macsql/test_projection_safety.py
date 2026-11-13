"""Offline counterexamples: saved real replies plus synthetic output contracts."""
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
from .dataset import Question
from .output_requirements import assess_requirements, bind_report, extraction_prompt, freeze_requirements
from .output_columns import project_required_columns
from .reference_baseline import ReferenceMacSQLAgent
from .reference_core import ModelReply, ROLES, WorkflowConfig
from .semantic_review import _review_sql
from .review_contract import parse_report, ReviewProtocolError
from .semantic_guard import RevisionRejected
from .test_output_columns import proposed_report
from .tools import SQLiteTools

REAL = json.loads((Path(__file__).with_name('fixtures')/'semantic-review-v6-q165-real.json').read_text(encoding='utf8'))


@pytest.fixture(autouse=True)
def no_cloud_requests(monkeypatch):
    import httpx
    def forbidden(*a, **kw):
        pytest.fail('Offline regression must never send HTTP requests')
    monkeypatch.setattr(httpx.Client, 'send', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden)


def typed_spec(q, outputs):
    return {'status':'clear','requested':[
        {'id':f'o{i}','source':'question','quote':quote,'description':description,
         'kind':'attribute','support':'explicit','representation':'not_applicable'}
        for i,(description,quote) in enumerate(outputs,1)], 'constraints':[], 'ambiguities':[]}


def test_actual_extraction_conflict_allows_inspection_but_blocks_removal():
    original=copy.deepcopy(REAL);q=Question(**REAL['question']);calls=[];events=[]
    def model(**kw):
        calls.append(kw)
        assert len(calls)<=2
        return ModelReply(REAL['extraction_raw'] if len(calls)==1 else REAL['review_raw'],311,247,simulated=True)
    tools=SimpleNamespace(diagnose_sql_readonly=lambda *a:{'status':'completed','findings':[]})
    agent=ReferenceMacSQLAgent({r:'offline-replay' for r in ROLES},config=WorkflowConfig(tools,model,trace=events.append,semantic_review=True))
    result=agent._semantic_review({'question':q,'run_id':'saved-extraction','sql':REAL['before'],
        'full_schema':REAL['schema'],'feedback':{'status':'ok'}})
    assert result['semantic_status']=='revision_guard_rejected'
    assert result['semantic_review_count']==1 and len(calls)==2
    assert 'sql' not in result and 'output_projection_suggestion' not in result
    assert any(r['code']=='shared_constraint_output_quote' for r in result['output_requirements_assessment']['risks'])
    assert REAL==original


def test_actual_second_reply_cannot_bypass_support_guard():
    q=Question(**REAL['question']);events=[]
    frozen=freeze_requirements(REAL['extraction_raw'],q)
    tools=SimpleNamespace(diagnose_sql_readonly=lambda *a:{'status':'completed','findings':[]})
    model=lambda **kw:ModelReply(REAL['review_raw'],3636,539,simulated=True)
    agent=ReferenceMacSQLAgent({r:'offline-replay' for r in ROLES},config=WorkflowConfig(tools,model,trace=events.append,semantic_review=True))
    result=_review_sql(agent,{'question':q,'run_id':'saved-review','sql':REAL['before'],
        'full_schema':REAL['schema'],'feedback':{'status':'ok'}},frozen)
    assert result['semantic_status']=='revision_guard_rejected'
    assert result['semantic_report']['guard']['code']=='output_support_unverified'
    assert 'sql' not in result and not any(e['event']=='output_columns_suggested' for e in events)


@pytest.mark.parametrize('mode',['inferred','unspecified','shared_quote'])
def test_model_clear_cannot_override_observable_risks(mode):
    q=Question('any-id','any-db','Return products ordered by revenue.')
    spec=typed_spec(q,[('products','Return products')]);item=spec['requested'][0]
    if mode=='inferred':item['support']='inferred'
    elif mode=='unspecified':item.update(kind='entity',representation='unspecified')
    else:
        item['quote']='ordered by revenue'
        spec['constraints']=[{'kind':'ordering','source':'question','quote':'ordered by revenue','description':'Sort revenue'}]
    assessed=assess_requirements(freeze_requirements(json.dumps(spec),q).spec)
    assert assessed['status']=='needs_review' and assessed['automatic_column_removal'] is False


def test_metric_can_be_requested_and_used_for_sorting_with_separate_support():
    q=Question('generic','db','Return names and revenue. Sort by revenue.')
    spec=typed_spec(q,[('names','names'),('revenue','Return names and revenue')])
    spec['requested'][1]['kind']='measure'
    spec['constraints']=[{'kind':'ordering','source':'question','quote':'Sort by revenue','description':'Sort revenue'}]
    assessed=assess_requirements(freeze_requirements(json.dumps(spec),q).spec)
    assert assessed['status']=='structurally_supported'
    assert assessed['semantic_correctness']=='not_verified'


@pytest.mark.parametrize('requested,sql,columns',[
    ('names and populations','SELECT name, population, id FROM t',('name','population')),
    ('names and IDs','SELECT name, id, score FROM t',('name','id')),
    ('names and postal codes','SELECT name, postal_code, id FROM t',('name','postal_code')),
    ('names and scores','SELECT name, score, id FROM t ORDER BY score DESC',('name','score')),
])
def test_explicit_multioutput_cannot_drop_second_requested_column(requested,sql,columns):
    q=Question('generic','db','Return '+requested+'.');report=proposed_report(sql,q,keep=(1,2))
    spec=typed_spec(q,[(x,q.question) for x in columns])
    report['output_contract'].pop('requested')
    bound=bind_report(report,freeze_requirements(json.dumps(spec),q),q)
    bound['output_contract']['columns'][1].update(disposition='extra',requested=[])
    with pytest.raises(RevisionRejected):project_required_columns(sql,q,bound)


@pytest.mark.parametrize('question,sql,keep',[
    ('Return IDs ordered by name.','SELECT id, name FROM t ORDER BY name',(1,)),
    ('Return names and scores.','SELECT name, score, id FROM t ORDER BY score',(1,2)),
    ('Return names and postal codes.','SELECT name, postal_code, id FROM t',(1,2)),
    # Deliberately wrong mapping: even a plausible-looking contract cannot authorize adoption.
    ('Return names and scores.','SELECT name, score FROM t',(2,)),
])
def test_suggestion_mode_never_executes_or_submits_proposed_projection(tmp_path,question,sql,keep):
    path=tmp_path/'synthetic.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE t(name TEXT,score INT,id TEXT,postal_code TEXT);"
                           "INSERT INTO t VALUES('A',7,'0001','00123');")
    tools=SQLiteTools({'db':path});q=Question('generic','db',question);events=[];calls=[]
    report=proposed_report(sql,q,keep=keep)
    for item in report['output_contract']['requested']:
        item.update(kind='attribute',support='explicit',representation='not_applicable')
    spec={'status':'clear','requested':copy.deepcopy(report['output_contract']['requested']), 'constraints':[], 'ambiguities':[]}
    def model(**kw):
        calls.append(kw)
        return ModelReply(json.dumps(spec if len(calls)==1 else report),1,1,simulated=True)
    state={'question':q,'run_id':'synthetic','sql':sql,'full_schema':tools.load_schema('db'),
           'feedback':tools.execute_sql_readonly('db',sql)}
    before=copy.deepcopy(state['feedback'])
    def no_candidate_execution(*a,**kw):raise AssertionError('Suggestion must not execute')
    tools.execute_sql_readonly=no_candidate_execution
    tools.compile_sql_readonly=no_candidate_execution
    agent=ReferenceMacSQLAgent({r:'offline-test' for r in ROLES},config=WorkflowConfig(tools,model,trace=events.append,semantic_review=True))
    result=agent._semantic_review(state)
    assert result['semantic_status']=='projection_suggested',result
    assert 'sql' not in result and 'feedback' not in result
    assert result['semantic_revision_count']==0 and not result['semantic_metrics']['sql_changed']
    assert not result['output_projection_suggestion']['executed']
    assert state['sql']==sql and state['feedback']==before
    assert len(calls)==2


def test_prompt_uses_generic_contrasts_and_no_specific_question_rules():
    prompt=extraction_prompt(Question('random','db','Return account codes.'))
    for snippet in ['customer IDs ordered by name','product names and revenue','representation=unspecified']:
        assert snippet in prompt
    assert '165' not in prompt and 'player_name' not in prompt and 'heading_accuracy' not in prompt


def test_documented_convention_requires_evidence_and_partial_type_fields_fail():
    q=Question('q','db','Return names.');spec=typed_spec(q,[('names','names')])
    spec['requested'][0]['support']='documented'
    with pytest.raises(ReviewProtocolError):freeze_requirements(json.dumps(spec),q)
    spec['requested'][0].pop('support')
    with pytest.raises(ReviewProtocolError):freeze_requirements(json.dumps(spec),q)


def test_graph_submits_original_and_persists_suggestion_separately(tmp_path):
    path=tmp_path/'graph.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE t(name TEXT,id INT);INSERT INTO t VALUES('A',1);")
    q=Question('generic','db','Return names.');before='SELECT name, id FROM t'
    report=proposed_report(before,q);spec=typed_spec(q,[('names','Return names')])
    report['output_contract']['requested']=spec['requested'];events=[]
    def model(**kw):
        if kw['role']=='decomposer':return ModelReply('```sql\n'+before+'\n```',1,1,simulated=True)
        is_extraction=kw['messages'][-1]['content'].startswith('OUTPUT_REQUIREMENTS_V3')
        return ModelReply(json.dumps(spec if is_extraction else report),1,1,simulated=True)
    agent=ReferenceMacSQLAgent({r:'offline-test' for r in ROLES},config=WorkflowConfig(
        SQLiteTools({'db':path}),model,trace=events.append,semantic_review=True))
    assert agent.run(q)==before
    completed=next(e for e in events if e['event']=='workflow_completed')
    assert completed['semantic_status']=='projection_suggested'
    assert completed['semantic_revision_count']==0
    assert completed['output_projection_suggestion']['sql']!=before
    assert completed['output_projection_suggestion']['adopted'] is False
    assert completed['output_requirements_assessment']['automatic_column_removal'] is False
