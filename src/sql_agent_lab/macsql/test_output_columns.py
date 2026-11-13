"""Real-response omissions and synthetic repair regressions, without API calls."""
import copy
import json
import sqlite3
from pathlib import Path
import pytest
import sqlglot
from .dataset import Question
from .output_columns import audit_output_columns, projection_inventory, project_required_columns
from .review_contract import parse_report, validate_requirements, CHECKS
from .semantic_guard import RevisionRejected

REAL = next(r for r in json.loads((Path(__file__).with_name('fixtures') /
    'semantic-review-deepseek-core6-real.json').read_text(encoding='utf8')) if r['question']['question_id']=='165')


def proposed_report(sql, question, keep=(1,)):
    """Hand-authored contract for a test, never attributed to a real model reply."""
    inventory = projection_inventory(sql)['columns']
    requested = [{'id': 'o'+str(i), 'source': 'question', 'quote': question.question,
                  'description': inventory[i-1]['name']} for i in keep]
    columns = [{**c, 'disposition': 'required' if c['position'] in keep else 'extra',
                'requested': ['o'+str(c['position'])] if c['position'] in keep else [],
                'reason': 'explicit requested output' if c['position'] in keep else 'internal sorting/grouping value, not requested',
                'basis': {'source':'question','quote':question.question}} for c in inventory]
    return {'action':'revise','projection_only':True,'changes':[],
        'output_contract':{'requested':requested,'columns':columns},
        'checks':{k:{'status':'fail' if k=='output' else 'pass','reason':'extra outputs' if k=='output' else 'retained'} for k in CHECKS},
        'requirements':[{'id':'r1','source':'question','quote':question.question,'check':'output','status':'fail',
                         'sql_fragment':inventory[0]['expression'],'reason':'Return only explicitly requested fields'}]}


def test_real_165_accept_does_not_establish_a_column_check():
    row = copy.deepcopy(REAL)
    result = audit_output_columns(row['before'],Question(**row['question']),parse_report(row['raw']))
    assert result['status']=='not_checked'
    assert [c['name'] for c in result['columns']]==['player_name','player_api_id','mean_heading']
    assert all('disposition' not in c for c in result['columns'])
    assert row==REAL


def test_165_guarded_projection_preserves_inner_query_and_tie_order(tmp_path):
    q=Question(**REAL['question']); before=REAL['before']; report=proposed_report(before,q)
    validate_requirements(parse_report(json.dumps(report)),q,before)
    candidate,audit=project_required_columns(before,q,report)
    old=sqlglot.parse_one(before,read='sqlite'); new=sqlglot.parse_one(candidate,read='sqlite')
    assert new.args['from_'].this.this==old
    assert audit['expected_columns']==1 and len(audit['ordering'])==2
    with sqlite3.connect(tmp_path/'synthetic.sqlite') as conn:
        conn.executescript('CREATE TABLE Player(player_name TEXT,player_api_id INT,height INT);'
          'CREATE TABLE Player_Attributes(player_api_id INT,heading_accuracy INT);'
          "INSERT INTO Player VALUES('Z',1,185),('A',2,186),('B',3,175),('C',4,190);"
          'INSERT INTO Player_Attributes VALUES(1,80),(1,100),(2,90),(3,100),(4,85);')
        original=conn.execute(before).fetchall(); actual=conn.execute(candidate).fetchall()
    assert actual==[(r[0],) for r in original]==[('Z',),('A',),('C',)]


def test_names_and_populations_cannot_drop_a_requested_population():
    q=Question('different','db','Return district names and populations.')
    sql='SELECT name, population, id FROM district ORDER BY population DESC'
    report=proposed_report(sql,q,keep=(1,2))
    candidate,_=project_required_columns(sql,q,report)
    assert len(sqlglot.parse_one(candidate).expressions)==2
    report['output_contract']['columns'][1].update(disposition='extra',requested=[])
    audit=audit_output_columns(sql,q,report)
    assert audit['missing_requested']==['o2']
    with pytest.raises(RevisionRejected): project_required_columns(sql,q,report)


@pytest.mark.parametrize('mode',['uncertain','missing_column','wrong_expression','duplicate_position','bad_quote','requested_and_extra','mixed_changes','other_failed_check','invented_output'])
def test_invalid_or_ambiguous_contract_cannot_delete_columns(mode):
    q=Question('q','db','Return names and postal codes.');sql='SELECT name, postal_code, id FROM people'
    report=proposed_report(sql,q,keep=(1,2)); columns=report['output_contract']['columns']
    if mode=='uncertain': columns[2]['disposition']='uncertain'
    elif mode=='missing_column': columns.pop()
    elif mode=='wrong_expression': columns[2]['expression']='nonexistent'
    elif mode=='duplicate_position': columns[2]['position']=1
    elif mode=='bad_quote': columns[2]['basis']['quote']='Return only names'
    elif mode=='requested_and_extra': columns[1]['disposition']='extra'
    elif mode=='mixed_changes': report['changes']=[{'before':'people','after':'other'}]
    elif mode=='other_failed_check': report['checks']['filters']['status']='fail'
    else: report['output_contract']['requested'][0]['quote']='Return account balances'
    with pytest.raises(RevisionRejected): project_required_columns(sql,q,report)


@pytest.mark.parametrize('sql', ['SELECT * FROM people','SELECT p.* FROM people p',
 'SELECT name FROM a UNION SELECT name FROM b','SELECT name FROM a; SELECT name FROM b'])
def test_unsupported_projections_are_not_guessed(sql):
    assert projection_inventory(sql)['status']=='unsupported'


@pytest.mark.parametrize('sql', ['SELECT name, name FROM people',
 'SELECT name, SUM(n) FROM people', 'SELECT name, id FROM people ORDER BY unreturned_score DESC'])
def test_ambiguous_names_and_unavailable_sort_keys_block_wrapper(sql):
    q=Question('q','db','Return names.'); report=proposed_report(sql,q)
    with pytest.raises(RevisionRejected): project_required_columns(sql,q,report)


@pytest.mark.parametrize('sql',[
 'SELECT DISTINCT name, id FROM people ORDER BY id',
 'SELECT name, id FROM people ORDER BY 2 DESC LIMIT 2',
 'SELECT COUNT(*) AS total, MAX(id) AS highest FROM people',
])
def test_wrapper_preserves_distinct_aggregate_and_ordinal_semantics(sql):
    q=Question('q','db','Return the first requested output.'); report=proposed_report(sql,q)
    candidate,_=project_required_columns(sql,q,report)
    with sqlite3.connect(':memory:') as conn:
        conn.executescript("CREATE TABLE people(name TEXT,id INT);INSERT INTO people VALUES('A',1),('A',2),('B',3);")
        assert conn.execute(candidate).fetchall()==[(r[0],) for r in conn.execute(sql).fetchall()]


def test_required_text_identifiers_and_postcodes_preserved():
    q=Question('q','db','Return names and postal codes.');sql='SELECT name, postal_code, id FROM people ORDER BY id'
    candidate,_=project_required_columns(sql,q,proposed_report(sql,q,keep=(1,2)))
    with sqlite3.connect(':memory:') as conn:
        conn.executescript("CREATE TABLE people(name TEXT,postal_code TEXT,id INT);INSERT INTO people VALUES('A','00123',1);")
        assert conn.execute(candidate).fetchall()==[('A','00123')]


def test_projection_is_saved_as_suggestion_without_replacing_or_executing(tmp_path):
    from .reference_baseline import ReferenceMacSQLAgent
    from .reference_core import WorkflowConfig,ModelReply,ROLES
    from .tools import SQLiteTools
    p=tmp_path/'synthetic.sqlite'
    with sqlite3.connect(p) as conn:
        conn.executescript("CREATE TABLE people(name TEXT,postal_code TEXT,id INT);INSERT INTO people VALUES('A','00123',1);")
    tools=SQLiteTools({'db':p}); q=Question('q','db','Return names and postal codes.')
    before='SELECT name, postal_code, id FROM people ORDER BY id'
    report=proposed_report(before,q,keep=(1,2));events=[]
    for item in report['output_contract']['requested']:
        item.update(kind='attribute',support='explicit',representation='not_applicable')
    def model(**kwargs):
        value={'status':'clear','requested':report['output_contract']['requested'],'constraints':[],'ambiguities':[]} if kwargs['messages'][-1]['content'].startswith('OUTPUT_REQUIREMENTS_V3') else report
        return ModelReply(json.dumps(value),1,1,simulated=True)
    agent=ReferenceMacSQLAgent({r:'offline-test' for r in ROLES},config=WorkflowConfig(tools,model,trace=events.append,semantic_review=True))
    result=agent._semantic_review({'run_id':'offline-columns','question':q,'sql':before,
        'full_schema':tools.load_schema('db'),'feedback':tools.execute_sql_readonly('db',before)})
    assert result['semantic_status']=='projection_suggested',result
    assert 'sql' not in result and 'feedback' not in result
    assert result['output_projection_suggestion']['adopted'] is False
    assert result['output_projection_suggestion']['executed'] is False
    assert any(e['event']=='output_columns_suggested' for e in events)
    assert not any(e['event']=='output_type_checked' and e['phase']=='semantic_revision' for e in events)
