"""Synthetic output contracts: quantities versus identifier representations."""
import sqlite3
import pytest
from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits
from sql_agent_lab.macsql.output_types import check_output_types, infer_contract
from sql_agent_lab.macsql.reference_core import WorkflowConfig, ModelReply, ROLES
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent
from sql_agent_lab.macsql.dataset import Question


@pytest.fixture
def source(tmp_path):
    db=tmp_path/'synthetic.sqlite'
    with sqlite3.connect(db) as conn:
        conn.executescript('CREATE TABLE regions (region_id TEXT PRIMARY KEY, label TEXT, metric TEXT, postal_code TEXT, phone TEXT, price TEXT);')
        conn.executemany('INSERT INTO regions VALUES(?,?,?,?,?,?)',[
            ('0007','Alpha','128','00501','0012345678','12.75'),
            ('0008','Beta','256','00901','0000123456','7.25')])
    metadata={'db':{'regions':{'metric':{'description':'number of inhabitants','source':'synthetic documentation'},
                              'label':{'description':'region_name','source':'synthetic documentation'}}}}
    tools=SQLiteTools({'db':db},metadata=metadata)
    return tools,tools.load_schema('db',enrich=True)


def inspect(source,sql,explicit=()):
    tools,schema=source
    feedback=tools.execute_sql_readonly('db',sql)
    assert feedback['status']=='ok'
    return check_output_types(sql,feedback,schema,explicit),feedback


def test_cast_only_in_comparison_does_not_change_projection_type(source):
    before='WITH cohort AS (SELECT label, metric FROM regions) SELECT label, metric FROM cohort WHERE CAST(metric AS INTEGER)=(SELECT MAX(CAST(metric AS INTEGER)) FROM cohort)'
    report,feedback=inspect(source,before)
    assert feedback['preview_rows']==[['Beta','256']]
    assert report['status']=='mismatch'
    assert report['columns'][1]['expected']=='integer'
    after=before.replace('SELECT label, metric FROM cohort WHERE','SELECT label, CAST(metric AS INTEGER) AS metric FROM cohort WHERE')
    report,feedback=inspect(source,after)
    assert report['status']=='ok'
    assert feedback['preview_rows']==[['Beta',256]]


@pytest.mark.parametrize('column',['region_id','postal_code','phone'])
def test_text_identifiers_preserved_and_numeric_cast_rejected(source,column):
    report,feedback=inspect(source,f'SELECT {column} FROM regions')
    assert report['status']=='ok'
    assert all(isinstance(row[0],str) and row[0].startswith('0') for row in feedback['preview_rows'])
    report,_=inspect(source,f'WITH t AS (SELECT {column} AS value FROM regions) SELECT CAST(value AS INTEGER) AS amount FROM t')
    assert report['status']=='mismatch'
    assert report['columns'][0]['protected_identifier']
    # Casting back to TEXT cannot restore a lost leading zero.
    report,_=inspect(source,f'SELECT CAST(CAST({column} AS INTEGER) AS TEXT) FROM regions')
    assert report['status']=='mismatch'


def test_count_of_identifiers_is_integer_not_identifier(source):
    report,feedback=inspect(source,'SELECT COUNT(region_id), LENGTH(postal_code) FROM regions')
    assert report['status']=='ok'
    assert feedback['preview_rows'][0][0]==2
    report,_=inspect(source,'SELECT CAST(COUNT(postal_code) AS TEXT) FROM regions')
    assert report['status']=='mismatch'
    assert not report['columns'][0]['protected_identifier']
    report,_=inspect(source,'SELECT ROW_NUMBER() OVER (ORDER BY postal_code) FROM regions')
    assert report['status']=='ok'


def test_native_integer_identifier_is_not_forced_to_text(tmp_path):
    db=tmp_path/'native.sqlite'
    with sqlite3.connect(db) as conn:
        conn.executescript('CREATE TABLE rows(id INTEGER PRIMARY KEY); INSERT INTO rows VALUES(7);')
    tools=SQLiteTools({'db':db})
    report,feedback=inspect((tools,tools.load_schema('db')),'SELECT id FROM rows')
    assert report['status']=='unverified'
    assert feedback['preview_rows']==[[7]]
    assert report['automatic_conversion'] is False


def test_explicit_formatting_contract_can_preserve_quantity_as_text(source):
    report,feedback=inspect(source,'SELECT metric FROM regions',('text',))
    assert report['status']=='ok'
    assert isinstance(feedback['preview_rows'][0][0],str)


def test_numeric_decimal_and_text_values_are_never_coerced(source):
    report,feedback=inspect(source,'SELECT price FROM regions')
    assert report['status']=='mismatch'
    assert feedback['preview_rows'][0]==['12.75']
    report,feedback=inspect(source,'SELECT CAST(price AS REAL) FROM regions')
    assert report['status']=='ok' and feedback['preview_rows'][0]==[12.75]


def test_unknown_semantics_are_not_inferred_from_digits(source):
    tools,schema=source
    schema['regions']['columns']['metric']={'type':'TEXT'}
    report,feedback=inspect((tools,schema),'SELECT metric FROM regions')
    assert report['status']=='unverified'
    assert feedback['preview_rows']==[['128'],['256']]


def test_positional_contract_and_identifier_conflict(source):
    report,_=inspect(source,'SELECT metric FROM regions',('integer',))
    assert report['status']=='mismatch'
    report,_=inspect(source,'SELECT postal_code FROM regions',('integer',))
    assert report['status']=='mismatch' and report['columns'][0]['contract_conflict']
    report,_=inspect(source,'SELECT label, metric FROM regions',('text',))
    assert report['status']=='mismatch'


def test_null_and_empty_results_are_unverified_not_zero(source):
    report,feedback=inspect(source,'SELECT NULL AS result',('number',))
    assert report['status']=='unverified' and feedback['preview_rows']==[[None]]
    report,feedback=inspect(source,'SELECT CAST(metric AS INTEGER) FROM regions WHERE 1=0')
    assert report['status']=='unverified' and feedback['preview_rows']==[]


def test_raw_profiles_detect_mixed_values_beyond_display_preview(tmp_path):
    db=tmp_path/'mixed.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE observations(value)')
        conn.executemany('INSERT INTO observations VALUES(?)',[(i,) for i in range(6)]+[('7',),('bad',),(None,)])
    tools=SQLiteTools({'db':db})
    feedback=tools.execute_sql_readonly('db','SELECT value FROM observations')
    assert len(feedback['preview_rows'])==5
    report=check_output_types('SELECT value FROM observations',feedback,tools.load_schema('db'),('number',))
    assert report['status']=='mismatch' and report['scope']=='complete'
    item=report['columns'][0]
    assert item['storage_classes']=={'integer':6,'text':2,'null':1}
    assert item['numeric_text_count']==1 and item['other_text_count']==1
    # Exceeding the sample cap must not be presented as a complete pass.
    bounded=SQLiteTools({'db':db},ToolLimits(type_sample_rows=3))
    feedback=bounded.execute_sql_readonly('db','SELECT value FROM observations')
    report=check_output_types('SELECT value FROM observations',feedback,tools.load_schema('db'),('number',))
    assert report['scope']=='bounded_sample' and report['status']=='unverified'


def test_unsupported_lineage_and_duplicate_aliases_are_safe(source):
    report,_=inspect(source,'SELECT metric AS same, postal_code AS same FROM regions')
    assert report['columns'][0]['expected']=='integer'
    assert report['columns'][1]['expected']=='text'
    report,_=inspect(source,'SELECT label FROM regions UNION SELECT postal_code FROM regions')
    assert report['status']=='unverified' and 'analysis_note' in report


def test_type_mismatch_routes_to_refiner_and_preserves_postal(source):
    tools,_=source;events=[];calls=[]
    def model(**kwargs):
        calls.append(kwargs)
        sql='SELECT metric, postal_code FROM regions' if kwargs['role']=='decomposer' else 'SELECT CAST(metric AS INTEGER), postal_code FROM regions'
        return ModelReply('```sql\n'+sql+'\n```',0,0,simulated=True)
    agent=ReferenceMacSQLAgent({r:'synthetic' for r in ROLES},config=WorkflowConfig(tools,model,trace=events.append))
    sql=agent.run(Question('synthetic','db','Return the number of inhabitants and postal code for each region'))
    assert [c['role'] for c in calls]==['decomposer','refiner']
    assert 'output_type_check' in calls[-1]['messages'][-1]['content']
    assert tools.execute_sql_readonly('db',sql)['preview_rows']==[[128,'00501'],[256,'00901']]
    assert [e['report']['status'] for e in events if e['event']=='output_type_checked']==['mismatch','ok']
    assert next(e for e in events if e['event']=='workflow_completed')['output_type_check']['status']=='ok'


def test_unchanged_refiner_output_stays_unresolved_and_stops(source):
    tools,_=source;events=[];calls=[]
    def model(**kwargs):
        calls.append(kwargs)
        return ModelReply('```sql\nSELECT metric FROM regions\n```',0,0,simulated=True)
    agent=ReferenceMacSQLAgent({r:'synthetic' for r in ROLES},config=WorkflowConfig(tools,model,trace=events.append))
    agent.run(Question('synthetic','db','Return inhabitants'))
    assert len(calls)==2
    assert next(e for e in events if e['event']=='workflow_completed')['output_type_check']['status']=='mismatch'
