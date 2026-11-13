import json
import sqlite3
from pathlib import Path
import pytest
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent
from sql_agent_lab.macsql.reference_core import WorkflowConfig, ModelReply, ROLES
from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql.tools import SQLiteTools
from sql_agent_lab.macsql.semantic_review import CHECKS, parse_report
from sql_agent_lab.macsql import semantic_guard
from sql_agent_lab.macsql.semantic_guard import apply_revision, evidence_context, inspect_candidate, RevisionRejected, signature


def report(action='accept', sql=None):
    r = {'action': action, 'checks': {k:{'status':'pass','reason':'Matches the requirement'} for k in CHECKS}}
    status = 'fail' if action == 'revise' else 'uncertain' if action == 'uncertain' else 'pass'
    r['requirements'] = [{'id':'r1','quote':'Give the largest number','check':'ordering',
                          'status':status,'sql_fragment':'ORDER BY n ASC LIMIT 1','reason':'Check direction'}]
    if action == 'uncertain':
        r['checks']['ordering']['status'] = 'uncertain'
    if action=='revise':
        r['checks']['ordering']={'status':'fail','reason':'Question says largest; SQL uses ASC'}
        r['changes']=[{'checks':['ordering'], 'requirements':['r1'], 'before':'SELECT n FROM numbers ORDER BY n ASC LIMIT 1',
                       'after':sql, 'requirement_quote':'largest number',
                       'evidence':[{'source':'question','quote':'Give the largest number'}]}]
    r['output_contract'] = {'requested':[{'id':'o1','source':'question','quote':'largest number','description':'number'}],
        'columns':[{'position':1,'expression':'n','disposition':'required','requested':['o1'],'reason':'requested number'}]}
    return r


@pytest.mark.parametrize('action,candidate,expected', [
    ('revise','SELECT n FROM numbers ORDER BY n DESC LIMIT 1','SELECT n FROM numbers ORDER BY n DESC LIMIT 1'),
    ('revise','SELECT n FROM numbers ORDER BY n ASC LIMIT 1','SELECT n FROM numbers ORDER BY n ASC LIMIT 1'),
    ('accept',None,'SELECT n FROM numbers ORDER BY n ASC LIMIT 1'),
    ('uncertain',None,'SELECT n FROM numbers ORDER BY n ASC LIMIT 1'),
    ('revise','DELETE FROM numbers','SELECT n FROM numbers ORDER BY n ASC LIMIT 1'),
    ('revise','SELECT invented FROM numbers','SELECT n FROM numbers ORDER BY n ASC LIMIT 1'),
    ('broken',None,'SELECT n FROM numbers ORDER BY n ASC LIMIT 1')])
def test_audits_executable_sql_and_keeps_original_on_failed_revision(tmp_path,action,candidate,expected):
    path=tmp_path/'db.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE numbers(n INTEGER); INSERT INTO numbers VALUES(1),(9);')
    calls=[]; events=[]
    def model(**kw):
        calls.append(kw)
        if kw['messages'][-1]['content'].startswith('OUTPUT_REQUIREMENTS_V3'):
            return ModelReply(json.dumps({'status':'clear','requested':report()['output_contract']['requested'],'constraints':[],'ambiguities':[]}),10,10,simulated=True)
        response='```sql\nSELECT n FROM numbers ORDER BY n ASC LIMIT 1\n```' if kw['role']=='decomposer' else (
            'not JSON' if action=='broken' else json.dumps(report(action,candidate)))
        return ModelReply(response,10,10,simulated=True)
    agent=ReferenceMacSQLAgent({r:'test' for r in ROLES},config=WorkflowConfig(
        SQLiteTools({'db':path}),model,semantic_review=True,trace=events.append))
    assert agent.run(Question('q','db','Give the largest number'))==expected
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM numbers').fetchone()[0] == 2
    assert [c['role'] for c in calls]==['decomposer','refiner','refiner']
    assert [c['max_output_tokens'] for c in calls] == [1000, 800, 1600]
    assert 'status="pass" and reason="not required"' in calls[-1]['messages'][-1]['content']
    assert 'reference_sql' not in calls[-1]['messages'][-1]['content']
    if action=='broken':
        assert any(e['event']=='semantic_review_error' for e in events)
    if candidate and 'invented' in candidate:
        assert next(e for e in events if e['event']=='semantic_revision_checked')['adopted'] is False
    if action=='revise' and candidate=='SELECT n FROM numbers ORDER BY n ASC LIMIT 1':
        assert next(e for e in events if e['event']=='semantic_revision_guard')['code']=='no_effect'
        assert next(e for e in events if e['event']=='workflow_completed')['semantic_status']=='revision_guard_rejected'


def test_semantic_budget_reserves_output_before_fitting_input():
    from sql_agent_lab.macsql.context import ContextPolicy, ContextBudgetExceeded
    policy = ContextPolicy(input_budget=14000, context_window=16384,
                           count_tokens=lambda messages: 14500)
    agent = ReferenceMacSQLAgent({r:'test' for r in ROLES}, config=WorkflowConfig(
        None, lambda **kwargs: None, context=policy, semantic_review=True))
    with pytest.raises(ContextBudgetExceeded):
        agent._fit('semantic_review', lambda schema: 'prompt', {})
    policy = ContextPolicy(input_budget=14000, context_window=16384,
                           count_tokens=lambda messages: 13500)
    agent = ReferenceMacSQLAgent({r:'test' for r in ROLES}, config=WorkflowConfig(
        None, lambda **kwargs: None, context=policy, semantic_review=True, semantic_output_tokens=2500))
    # 13500 fits the generation budget but not the reserved review budget (13372).
    assert agent._fit('decomposer', lambda schema: 'prompt', {})[1]['max_output_tokens'] == 1000
    with pytest.raises(ContextBudgetExceeded):
        agent._fit('semantic_review', lambda schema: 'prompt', {})


@pytest.mark.parametrize('budget', [0, 9000, True, 1600.0])
def test_invalid_semantic_output_budget(budget):
    with pytest.raises(ValueError, match='Semantic output budget'):
        WorkflowConfig(None, lambda **kwargs: None, semantic_output_tokens=budget)


def apply(r, before='SELECT n FROM numbers ORDER BY n ASC LIMIT 1'):
    q=Question('arbitrary','db','Give the largest number')
    return apply_revision(before,r,q,{'question':q.question,'evidence':''})


def test_real_responses_reject_unchanged_and_unsupported_rewrites():
    fixtures=json.loads((Path(semantic_guard.__file__).parent/'fixtures/semantic-review-v1-real.json').read_text(encoding='utf8'))
    expected={'2':'missing_changes','23':'unsupported_literal','50':'no_effect'}
    for f in fixtures:
        q=Question(**f['question'])
        with pytest.raises(RevisionRejected) as exc:
            apply_revision(f['before'],parse_report(json.dumps(f['report'])),q,
                           {'question':q.question,'evidence':q.evidence})
        assert exc.value.code==expected[q.question_id]


def test_local_patch_is_applied_and_unchanged_limit_is_rejected():
    r=report('revise','ORDER BY n DESC LIMIT 1')
    r['changes'][0]['before']='ORDER BY n ASC LIMIT 1'
    assert apply(r)=='SELECT n FROM numbers ORDER BY n DESC LIMIT 1'
    r['changes'][0]['after']='ORDER BY n ASC /* no correction */ LIMIT 1'
    with pytest.raises(RevisionRejected,match='does not change'):
        apply(r)


@pytest.mark.parametrize('mutation,code',[
    (lambda r:r['checks']['joins'].update(status='uncertain'),'unresolved_checks'),
    (lambda r:r['checks']['filters'].update(status='fail'),'unaddressed_checks'),
    (lambda r:r['changes'][0].update(requirement_quote='nonexistent requirement'),'ungrounded_requirement'),
    (lambda r:r['changes'][0]['evidence'][0].update(quote='invented documentation'),'ungrounded_evidence'),
    (lambda r:r['changes'][0]['evidence'][0].update(source='observed_samples'),'ungrounded_evidence'),
    (lambda r:r.update(sql='SELECT 123'),'conflicting_sql'),
    (lambda r:r['changes'].append(dict(r['changes'][0])),'overlapping_edits'),
])
def test_unsubstantiated_or_incomplete_changes_are_not_applied(mutation,code):
    r=report('revise','SELECT n FROM numbers ORDER BY n DESC LIMIT 1')
    mutation(r)
    with pytest.raises(RevisionRejected) as exc:apply(r)
    assert exc.value.code==code


def test_ambiguous_fragments_require_context():
    r=report('revise','DESC');r['changes'][0]['before']='ASC'
    with pytest.raises(RevisionRejected) as exc:
        apply(r,'SELECT n FROM numbers ORDER BY n ASC, n ASC')
    assert exc.value.code=='ambiguous_fragment'


def test_token_comparison_preserves_strings_and_quoted_identifiers():
    assert signature("SELECT 'a -- b', \"x y\" /*c*/ FROM t;")==signature("select 'a -- b',\"x y\" from t")
    assert signature("SELECT 'A B'")!=signature("SELECT 'AB'")
    assert signature("SELECT 'A'")!=signature("SELECT 'a'")
    assert signature('SELECT "a b" FROM t')!=signature('SELECT "ab" FROM t')
    assert signature("SELECT 'can''t'")!=signature("SELECT 'cant'")


def test_new_category_must_have_cited_support_not_samples():
    before="SELECT n FROM numbers WHERE kind='old'"
    candidate="SELECT n FROM numbers WHERE kind='unrelated'"
    with pytest.raises(RevisionRejected,match='lacks'):
        inspect_candidate(before,candidate,{'question':'Return the approved category'})
    inspect_candidate(before,candidate,{'evidence':'unrelated means the approved category'})


def test_review_context_preserves_documented_meaning_and_excludes_samples():
    q=Question('q','db','Give the largest number')
    schema={'numbers':{'foreign_keys':[], 'columns':{
        'n':{'type':'INTEGER','description':'An aggregate total, not a row count',
             'documentation_source':'metadata.csv','observed_samples':['misleading'],
             'enum_meanings':{'a':'all records'}},
        'other':{'type':'TEXT','description':'unrelated'},
        'truncated':{'type':'TEXT','description':'partial','documentation_source':'metadata.csv',
                     'documentation_truncated':True}}}}
    clean,sources,catalog=evidence_context(q,schema,'SELECT n, truncated FROM numbers')
    assert 'misleading' not in json.dumps([clean,sources,catalog])
    assert 'An aggregate total' in json.dumps(catalog)
    assert 'partial' not in json.dumps(catalog)
    assert 'enum_meanings' in json.dumps(catalog)
    assert set(clean['numbers']['columns'])==set(schema['numbers']['columns'])


def test_tie_correction_really_changes_executed_rows(tmp_path):
    path=tmp_path/'ties.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE tickets(name TEXT, amount INTEGER); '
                           "INSERT INTO tickets VALUES('a',9),('b',9),('c',1);")
    before='SELECT name FROM tickets ORDER BY amount DESC LIMIT 1'
    q=Question('independent','db','Return all names tied for the largest amount')
    r=report()
    r['action']='revise'
    r['checks']['limit']={'status':'fail','reason':'The top row limit drops ties'}
    r['requirements']=[{'id':'r1','quote':q.question,'check':'limit','status':'fail',
                        'sql_fragment':'LIMIT 1','reason':'Ties must all be returned'}]
    r['changes']=[{'checks':['limit'],'requirements':['r1'],'before':'ORDER BY amount DESC LIMIT 1',
                   'after':'WHERE amount = (SELECT MAX(amount) FROM tickets)',
                   'requirement_quote':'all names tied',
                   'evidence':[{'source':'question','quote':q.question}]}]
    r['output_contract']={'requested':[{'id':'o1','source':'question','quote':'all names','description':'names'}],
        'columns':[{'position':1,'expression':'name','disposition':'required','requested':['o1'],'reason':'requested names'}]}
    tools=SQLiteTools({'db':path}); events=[]
    def model(**kwargs):
        value={'status':'clear','requested':r['output_contract']['requested'],'constraints':[],'ambiguities':[]} if kwargs['messages'][-1]['content'].startswith('OUTPUT_REQUIREMENTS_V3') else r
        return ModelReply(json.dumps(value),1,1,simulated=True)
    agent=ReferenceMacSQLAgent({role:'test' for role in ROLES},config=WorkflowConfig(tools,model,trace=events.append,semantic_review=True))
    result=agent._semantic_review({'run_id':'synthetic-tie-regression','question':q,'sql':before,'full_schema':tools.load_schema('db'),
                                  'feedback':tools.execute_sql_readonly('db',before)})
    assert result['semantic_status']=='revised_unverified'
    assert tools.execute_sql_readonly('db',before)['preview_rows']==[['a']]
    assert result['feedback']['preview_rows']==[['a'],['b']]


def test_accept_cannot_hide_failed_checks():
    r=report(); r['checks']['joins']['status']='fail'
    with pytest.raises(ValueError,match='conflicts'):
        parse_report(json.dumps(r))


def test_fenced_json_and_uncertainty():
    r=report('uncertain');r['checks']['joins']['status']='uncertain'
    assert parse_report('```json\n'+json.dumps(r)+'\n```')['action']=='uncertain'
