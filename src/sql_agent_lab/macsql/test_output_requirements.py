"""Independent requirement extraction and schema-source regressions; no API."""
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
from .dataset import Question
from .output_requirements import freeze_requirements, extraction_prompt, bind_report, extract
from .review_contract import parse_report, validate_requirements, ReviewProtocolError
from .semantic_guard import evidence_context, RevisionRejected
from .output_columns import project_required_columns
from .test_output_columns import proposed_report
from .reference_baseline import ReferenceMacSQLAgent
from .reference_core import WorkflowConfig, ModelReply, ROLES

REAL=json.loads((Path(__file__).with_name('fixtures')/'semantic-review-v5-q165-real.json').read_text(encoding='utf8'))


def spec(question, outputs=('requested output',), ambiguous=False):
    return {'status':'ambiguous' if ambiguous else 'clear',
        'requested':[{'id':f'o{i}','source':'question','quote':question.question,'description':description}
                     for i,description in enumerate(outputs,1)],
        'constraints':[], 'ambiguities':['Output scope unspecified'] if ambiguous else []}


def test_question_only_request_stays_identical_for_different_candidate_sql():
    q=Question(**REAL['question']);captured=[]
    def model(**kw):
        captured.append(kw['messages'])
        return ModelReply(json.dumps(spec(q,ambiguous=True)),1,1,simulated=True)
    agent=ReferenceMacSQLAgent({r:'mock' for r in ROLES},config=WorkflowConfig(None,model,semantic_review=True))
    for sql in (REAL['before'],'SELECT secret_column FROM secret_schema_table'):
        result=extract(agent, {'run_id':'offline','question':q,'sql':sql,
            'full_schema':{'SECRET_SCHEMA':'must never enter extraction'},'feedback':{'preview_rows':['SECRET_PREVIEW']}})
        assert result.spec['status']=='ambiguous'
    assert captured[0]==captured[1]
    prompt=json.dumps(captured[0])
    assert all(s not in prompt for s in ('secret_column','SECRET_SCHEMA','SECRET_PREVIEW','AVG(pa.heading_accuracy)'))


def test_frozen_contract_cannot_be_modified_or_reused_for_another_question():
    q=Question('a','db','Return name and population.')
    frozen=freeze_requirements(json.dumps(spec(q,('name','population'))),q)
    exposed=frozen.spec;exposed['requested'].pop()
    assert len(frozen.spec['requested'])==2
    report={'output_contract':{'columns':[]}}
    bound=bind_report(report,frozen,q)
    assert len(bound['output_contract']['requested'])==2 and 'requested' not in report['output_contract']
    with pytest.raises(ReviewProtocolError): bind_report(report,frozen,Question('b','db','Return name only.'))
    bound['output_contract']['requested'].pop()
    with pytest.raises(ReviewProtocolError,match='cannot rewrite'): bind_report(bound,frozen,q)


@pytest.mark.parametrize('mode',['duplicate','wrong_quote','sql_field','invalid_status'])
def test_invalid_extraction_fails_before_sql_check(mode):
    q=Question('q','db','Return names.');value=spec(q)
    if mode=='duplicate': value['requested']*=2
    elif mode=='wrong_quote': value['requested'][0]['quote']='Return population'
    elif mode=='sql_field': value['requested'][0]['sql']='SELECT name'
    elif mode=='invalid_status': value['status']='accept'
    else: value['ambiguities']=['Unclear']
    calls=[]
    def model(**kw): calls.append(kw);return ModelReply(json.dumps(value),1,1,simulated=True)
    agent=ReferenceMacSQLAgent({r:'mock' for r in ROLES},config=WorkflowConfig(None,model,semantic_review=True))
    result=agent._semantic_review({'run_id':'offline','question':q,'sql':'SELECT name FROM t'})
    assert len(calls)==1 and result['semantic_status']=='requirements_unverified'


@pytest.mark.parametrize('question,sql,outputs',[
    ('Return names and populations.','SELECT name, population, id FROM district',('name','population')),
    ('Return names and IDs.','SELECT name, id, score FROM people',('name','id')),
    ('Return names and postal codes.','SELECT name, postal_code, score FROM people',('name','postal_code')),
    ('Return names and scores, highest score first.','SELECT name, score, id FROM people ORDER BY score DESC',('name','score')),
])
def test_frozen_multiple_outputs_cannot_be_silently_dropped(question,sql,outputs):
    q=Question('generic','db',question);draft=proposed_report(sql,q,keep=(1,2))
    value=spec(q,outputs);value['requested']=draft['output_contract']['requested']
    frozen=freeze_requirements(json.dumps(value),q)
    # Checker may omit the echo: the program binds the fixed requested outputs.
    draft['output_contract'].pop('requested');bound=bind_report(draft,frozen,q)
    candidate,audit=project_required_columns(sql,q,bound)
    assert audit['expected_columns']==2
    bound['output_contract']['columns'][1].update(disposition='extra',requested=[])
    with pytest.raises(RevisionRejected): project_required_columns(sql,q,bound)


def test_real_165_reply_cannot_redefine_names_as_names_ids_and_metrics():
    q=Question(**REAL['question'])
    # Test fixture assumption only, not a claimed real extraction by the model.
    frozen=freeze_requirements(json.dumps(spec(q,('player name',))),q)
    with pytest.raises(ReviewProtocolError) as error:
        bind_report(parse_report(REAL['raw']),frozen,q)
    assert error.value.code=='output_requirements_changed'


def test_real_foreign_key_quote_is_recovered_from_actual_schema():
    original=copy.deepcopy(REAL);q=Question(**REAL['question'])
    _,sources,catalog=evidence_context(q,REAL['schema'],REAL['before'])
    report=parse_report(REAL['raw'])
    checked=validate_requirements(report,q,REAL['before'],sources)
    warning=next(w for w in checked['warnings'] if w['code']=='schema_source_recovered')
    assert warning['requirement']=='r7' and len(warning['matched_sources'])==1
    fact=next(c for c in catalog if c['source']==warning['matched_sources'][0])
    assert (fact['table'],fact['column'],fact['target_table'],fact['target_column'])==(
        'Player_Attributes','player_api_id','Player','player_api_id')
    assert REAL==original


def test_ambiguous_foreign_key_excerpt_needs_full_source_identity():
    q=Question(**REAL['question']);schema=copy.deepcopy(REAL['schema'])
    schema['Other_Attributes']=copy.deepcopy(schema['Player_Attributes'])
    sql=REAL['before']+' /* Other_Attributes */'
    # Candidate explicitly contains both tables; comments are not table evidence.
    sql=sql.replace('FROM Player AS p','FROM Player AS p JOIN Other_Attributes AS x ON x.player_api_id=p.player_api_id')
    _,sources,catalog=evidence_context(q,schema,sql)
    report=parse_report(REAL['raw'])
    with pytest.raises(ReviewProtocolError) as error: validate_requirements(report,q,sql,sources)
    assert error.value.code=='ambiguous_schema_reference'
    fact=next(c for c in catalog if c['kind']=='schema_foreign_key' and c['table']=='Player_Attributes' and c['column']=='player_api_id')
    report['requirements'][-1].update(source=fact['source'],quote=fact['text'].splitlines()[0])
    assert validate_requirements(report,q,sql,sources)['status']=='validated'
    report['requirements'][-1]['quote']='FK: Player_Attributes.player_api_id -> Wrong.id'
    with pytest.raises(ReviewProtocolError): validate_requirements(report,q,sql,sources)


def test_missing_foreign_key_target_is_not_invented():
    q=Question('q','db','Return names.');schema={'a':{'columns':{},'foreign_keys':[{'column':'id','table':'b','target':None}]}}
    _,sources,catalog=evidence_context(q,schema,'SELECT id FROM a')
    assert not catalog and set(sources)=={'question','evidence'}


def test_two_distinct_requests_bind_outputs_and_preserve_old_sql_on_drift():
    q=Question(**REAL['question']);calls=[];events=[]
    def model(**kw):
        calls.append(kw)
        return ModelReply(json.dumps(spec(q,('player name',))) if len(calls)==1 else REAL['raw'],1,1,simulated=True)
    tools=SimpleNamespace(diagnose_sql_readonly=lambda *a:{'status':'completed','findings':[]})
    agent=ReferenceMacSQLAgent({r:'mock' for r in ROLES},config=WorkflowConfig(tools,model,trace=events.append,semantic_review=True))
    result=agent._semantic_review({'run_id':'offline','question':q,'sql':REAL['before'],'full_schema':REAL['schema'],'feedback':{'status':'ok'}})
    assert len(calls)==2 and [c['max_output_tokens'] for c in calls]==[800,1600]
    assert calls[0]['messages'][-1]['content'].startswith('OUTPUT_REQUIREMENTS_V3')
    assert REAL['before'] not in calls[0]['messages'][-1]['content']
    assert REAL['before'] in calls[1]['messages'][-1]['content']
    assert 'player name' in calls[1]['messages'][-1]['content']
    assert result['semantic_report']['error_code']=='output_requirements_changed'
    assert result['semantic_report']['output_contract']['requested']==parse_report(REAL['raw'])['output_contract']['requested']
    assert 'sql' not in result
    assert [e['purpose'] for e in events if e['event']=='call_started']==['output_requirements','semantic_review']


def test_legacy_two_step_mock_cannot_authorize_column_removal(tmp_path):
    from .tools import SQLiteTools
    path=tmp_path/'synthetic.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript("CREATE TABLE Player(player_name TEXT,player_api_id INT,height INT);"
            "CREATE TABLE Player_Attributes(player_api_id INT,heading_accuracy INT);"
            "INSERT INTO Player VALUES('Alpha',1,185),('Beta',2,187);"
            "INSERT INTO Player_Attributes VALUES(1,90),(2,80);")
    q=Question(**REAL['question']);before=REAL['before'];report=proposed_report(before,q)
    value=spec(q,('player name',));value['requested']=report['output_contract'].pop('requested')
    calls=[];events=[]
    def model(**kw):
        calls.append(kw);return ModelReply(json.dumps(value if len(calls)==1 else report),1,1,simulated=True)
    tools=SQLiteTools({q.db_id:path})
    agent=ReferenceMacSQLAgent({r:'offline-mock' for r in ROLES},config=WorkflowConfig(tools,model,trace=events.append,semantic_review=True))
    result=agent._semantic_review({'run_id':'synthetic-165','question':q,'sql':before,
        'full_schema':tools.load_schema(q.db_id),'feedback':tools.execute_sql_readonly(q.db_id,before)})
    assert len(calls)==2 and result['semantic_status']=='revision_guard_rejected'
    assert 'sql' not in result and 'feedback' not in result
    assert result['semantic_report']['guard']['code']=='output_support_unverified'
    assert result['output_requirements_digest']==next(e['digest'] for e in events if e['event']=='output_requirements_frozen')
    assert REAL['before'] not in calls[0]['messages'][-1]['content']


@pytest.mark.parametrize('budget',[0,1601,True,800.0])
def test_extraction_output_budget_is_bounded(budget):
    with pytest.raises(ValueError,match='Requirements output budget'):
        WorkflowConfig(None,lambda **kw:None,requirements_output_tokens=budget)
