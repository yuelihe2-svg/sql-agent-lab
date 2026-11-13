"""Separate scoring without adopting proposals or making API requests."""
import copy
import json
import sqlite3
import pytest
from .dataset import Reference, Question
from .tools import SQLiteTools
from .run_deepseek_review import evaluate_suggestion
from .reference_baseline import ReferenceMacSQLAgent
from .reference_core import WorkflowConfig, ModelReply, ROLES
from .review_contract import CHECKS


@pytest.fixture
def database(tmp_path):
    path=tmp_path/'db.sqlite'
    with sqlite3.connect(path) as c:
        c.executescript('CREATE TABLE t(n INTEGER);INSERT INTO t VALUES(1),(9);')
    return SQLiteTools({'db':path}),Reference('db','SELECT n FROM t ORDER BY n DESC LIMIT 1')


@pytest.mark.parametrize('sql,score', [('SELECT MAX(n) FROM t',1),('SELECT MIN(n) FROM t',0),('SELECT missing FROM t',0)])
def test_proposal_scored_independently_without_mutating_update(database,sql,score):
    tools,reference=database
    update={'semantic_suggestion':{'sql':sql,'adopted':False,'executed':False}}
    before=copy.deepcopy(update)
    result=evaluate_suggestion(tools,reference,update)
    assert result['score']==score and not result['adopted']
    assert update==before


def test_absence_is_not_zero_or_fallback_score(database):
    assert evaluate_suggestion(*database,{})=={'status':'not_generated','score':None,'sql':None,'adopted':False}


@pytest.mark.parametrize('ambiguous',[False,True])
def test_non_projection_patch_is_also_suggestion_only(database,ambiguous):
    tools,reference=database;q=Question('q','db','Return the largest number.')
    sql='SELECT n FROM t ORDER BY n ASC LIMIT 1'
    spec={'status':'clear','requested':[{'id':'o1','source':'question','quote':'largest number','description':'number',
        'kind':'measure','support':'explicit','representation':'not_applicable'}],'constraints':[],'ambiguities':[]}
    if ambiguous: spec['ambiguities']=['Output scope is unresolved']
    report={'action':'revise','requirements':[{'id':'r1','source':'question','quote':'largest number','check':'ordering',
        'status':'fail','sql_fragment':'ORDER BY n ASC','reason':'descending required'}],
        'checks':{k:{'status':'fail' if k=='ordering' else 'pass','reason':'ordering direction' if k=='ordering' else 'not required'} for k in CHECKS},
        'output_contract':{'columns':[{'position':1,'expression':'n','disposition':'required','requested':['o1'],'reason':'requested number'}]},
        'changes':[{'requirements':['r1'],'checks':['ordering'],'before':'ORDER BY n ASC','after':'ORDER BY n DESC',
            'requirement_quote':'largest number','evidence':[{'source':'question','quote':'largest number'}]}]}
    def model(**kw):return ModelReply(json.dumps(spec if kw['messages'][-1]['content'].startswith('OUTPUT_REQUIREMENTS_V3') else report),1,1,simulated=True)
    agent=ReferenceMacSQLAgent({r:'offline-test' for r in ROLES},config=WorkflowConfig(tools,model,semantic_review=True,semantic_suggestion_only=True))
    result=agent._semantic_review({'question':q,'run_id':'test','sql':sql,'full_schema':tools.load_schema('db'),
        'feedback':tools.execute_sql_readonly('db',sql)})
    if ambiguous:
        assert result['semantic_status']=='revision_guard_rejected' and 'sql' not in result
        assert result['semantic_report']['guard']['code']=='output_support_unverified'
        assert not result.get('semantic_suggestion')
        return
    assert result['semantic_status']=='semantic_suggested' and 'sql' not in result
    assert not result['semantic_suggestion']['adopted'] and not result['semantic_suggestion']['executed']
    assert evaluate_suggestion(tools,reference,result)['score']==1
