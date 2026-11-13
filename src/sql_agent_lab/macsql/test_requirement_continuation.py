"""Offline replay of V7 extraction replies; synthetic continuations are labeled as such."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from .dataset import Question
from .output_requirements import freeze_requirements, assess_requirements, bind_report
from .reference_baseline import ReferenceMacSQLAgent
from .reference_core import WorkflowConfig, ModelReply, ROLES
from .review_contract import CHECKS, ReviewProtocolError
from .test_projection_safety import typed_spec
from .test_output_columns import proposed_report

CASES = json.loads((Path(__file__).with_name('fixtures') /
    'semantic-review-v7-core6-real.json').read_text(encoding='utf8'))['cases']


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import httpx
    import socket
    def forbidden(*a, **kw):
        raise AssertionError('Offline regressions cannot connect to a model or network')
    monkeypatch.setattr(httpx.Client, 'send', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def run_replay(q, sql, schema, raw, continuation):
    calls, events = [], []
    def model(**kw):
        calls.append(kw)
        assert len(calls) <= 2, 'No recovery calls or retries'
        return ModelReply(raw if len(calls) == 1 else continuation, 1, 1, simulated=True)
    # Tools intentionally have no mutation/execution methods: inspection cannot apply edits.
    tools = SimpleNamespace(diagnose_sql_readonly=lambda *a: {'status':'completed','findings':[]})
    agent = ReferenceMacSQLAgent({r:'offline-replay' for r in ROLES}, config=WorkflowConfig(
        tools, model, trace=events.append, semantic_review=True, semantic_suggestion_only=True))
    state = {'question':q,'run_id':'offline','sql':sql,'full_schema':schema,'feedback':{'status':'ok'}}
    before = copy.deepcopy(state)
    result = agent._semantic_review(state)
    assert state == before
    assert len(calls) == 2 and result['semantic_review_count'] == 1
    assert 'sql' not in result and 'feedback' not in result
    assert calls[0]['messages'][-1]['content'].startswith('OUTPUT_REQUIREMENTS_V3')
    assert sql not in calls[0]['messages'][-1]['content']
    assert sql in calls[1]['messages'][-1]['content']
    assert any(e['event'] == 'semantic_review_started' for e in events)
    return result, calls, events


@pytest.mark.parametrize('case', CASES, ids=lambda c:c['question']['question_id'])
def test_all_six_real_extractions_reach_sql_review_without_changing_sql(case):
    original = copy.deepcopy(case)
    raw = case['responses'][0]['raw']
    q = Question(**case['question'])
    frozen = freeze_requirements(raw, q)
    raw_spec = json.loads(raw)
    assert frozen.spec['requested'] == raw_spec['requested']
    assert frozen.spec['constraints'] == raw_spec['constraints']
    assert frozen.spec['ambiguities'] == raw_spec['ambiguities']
    assert hashlib.sha256(frozen.serialized.encode()).hexdigest() == frozen.digest
    if q.question_id in ('165','203'):
        assert frozen.spec['status'] == 'ambiguous'
        assert frozen.protocol_warnings == ('clear_with_ambiguities_normalized',)
    # Three runs never made a second request. This deliberately synthetic reply
    # tests routing only; it must not be reported as a real DeepSeek conclusion.
    synthetic = {'action':'uncertain','requirements':[
        {'id':'r1','source':'question','quote':q.question,'check':'output','status':'uncertain',
         'sql_fragment':'','reason':'Synthetic continuation: output interpretation unresolved'}],
        'checks':{k:{'status':'uncertain','reason':'Synthetic offline continuation'} for k in CHECKS},
        'changes':[]}
    continuation = case['responses'][1]['raw'] if len(case['responses']) == 2 else json.dumps(synthetic)
    result, calls, events = run_replay(q, case['before'], case['schema'], raw, continuation)
    assert not result.get('output_projection_suggestion') and not result.get('semantic_suggestion')
    if len(case['responses']) == 2:
        assert result['semantic_status'] in ('accepted','accepted_with_warnings'), result
    else:
        assert result['semantic_status'] == 'output_unverified', result
        assert result['semantic_validation']['status'] in ('validated','validated_with_warnings')
        assert result['output_requirements_assessment']['column_removal_blocked']
        assert any(e['event'] == 'semantic_review_scope_warning' for e in events)
        assert 'Output ambiguity does not stop the audit' in calls[1]['messages'][-1]['content']
    assert case == original


@pytest.mark.parametrize('status,ambiguities,warning', [
    ('clear',['Name or ID is unspecified'],'clear_with_ambiguities_normalized'),
    ('ambiguous',[],'ambiguous_without_explanation'),
])
def test_status_inconsistency_preserves_concerns_and_binds_immutable_outputs(status,ambiguities,warning):
    q = Question('generic','db','Return names and IDs.')
    spec = typed_spec(q,[('names','names'),('IDs','IDs')])
    spec.update(status=status,ambiguities=ambiguities)
    frozen = freeze_requirements(json.dumps(spec),q)
    assert frozen.spec['status'] == 'ambiguous'
    assert frozen.spec['ambiguities'] == ambiguities
    assert frozen.protocol_warnings == (warning,)
    assert assess_requirements(frozen.spec)['column_removal_blocked']
    bound = bind_report({'output_contract':{'columns':[]}},frozen,q)
    assert bound['output_contract']['requested'] == spec['requested']
    bound['output_contract']['requested'].pop()
    with pytest.raises(ReviewProtocolError,match='cannot rewrite'):
        bind_report(bound,frozen,q)


@pytest.mark.parametrize('risk',['ambiguous','inferred','unspecified','shared_quote'])
def test_continued_review_cannot_use_uncertainty_to_authorize_deletion(risk):
    q = Question('generic','db','Return names and IDs ordered by score.')
    sql = 'SELECT name, id, score FROM t ORDER BY score'
    report = proposed_report(sql,q,keep=(1,2))
    spec = typed_spec(q,[('names','names'),('IDs','IDs')])
    if risk == 'ambiguous': spec.update(status='clear',ambiguities=['Unresolved scope'])
    elif risk == 'inferred': spec['requested'][1]['support'] = 'inferred'
    elif risk == 'unspecified': spec['requested'][1].update(kind='entity',representation='unspecified')
    else:
        spec['requested'][1]['quote'] = 'ordered by score'
        spec['constraints'] = [{'kind':'ordering','source':'question','quote':'ordered by score','description':'Sort score'}]
    report['output_contract'].pop('requested')
    result, _, _ = run_replay(q,sql,{},json.dumps(spec),json.dumps(report))
    assert result['semantic_status'] == 'revision_guard_rejected', result
    assert result['semantic_report']['guard']['code'] == 'output_support_unverified'
    assert not result.get('output_projection_suggestion')


def test_model_accept_cannot_erase_unresolved_output_warning():
    q = Question('generic','db','Return names.')
    spec = typed_spec(q,[('names','names')]);spec.update(status='clear',ambiguities=['Unresolved scope'])
    report = {'action':'accept','requirements':[
        {'id':'r1','source':'question','quote':'names','check':'output','status':'pass',
         'sql_fragment':'name','reason':'Requested name'}],
        'checks':{k:{'status':'pass','reason':'not required' if k!='output' else 'name returned'} for k in CHECKS},
        'changes':[], 'output_contract':{'columns':[
            {'position':1,'expression':'name','disposition':'required','requested':['o1'],'reason':'Name'}]}}
    result, _, _ = run_replay(q,'SELECT name FROM t',{},json.dumps(spec),json.dumps(report))
    assert result['semantic_status'] == 'output_unverified'
    assert result['semantic_metrics']['outcome'] == 'output_interpretation_unresolved'


def test_output_ambiguity_does_not_hide_other_sql_findings():
    q = Question('generic','db','Return products with score above 10.')
    sql = 'SELECT name FROM t WHERE score > 5'
    spec = typed_spec(q,[('products','products')])
    spec['requested'][0].update(kind='entity',representation='unspecified')
    report = {'action':'uncertain','requirements':[
        {'id':'r1','source':'question','quote':'products','check':'output','status':'uncertain',
         'sql_fragment':'name','reason':'Entity representation unspecified'},
        {'id':'r2','source':'question','quote':'score above 10','check':'filters','status':'fail',
         'sql_fragment':'score > 5','reason':'Threshold is 5 instead of 10'}],
        'checks':{k:{'status':('uncertain' if k=='output' else 'fail' if k=='filters' else 'pass'),
                     'reason':'Output uncertain' if k=='output' else 'Wrong threshold' if k=='filters' else 'not required'} for k in CHECKS},
        'changes':[], 'output_contract':{'columns':[
            {'position':1,'expression':'name','disposition':'uncertain','requested':['o1'],'reason':'Representation unresolved'}]}}
    result, _, _ = run_replay(q,sql,{},json.dumps(spec),json.dumps(report))
    assert result['semantic_status'] == 'output_unverified'
    assert result['semantic_report']['checks']['filters']['status'] == 'fail'
    assert result['semantic_report']['requirements'][1]['reason'] == 'Threshold is 5 instead of 10'
    assert result['semantic_validation']['status'] == 'validated'
