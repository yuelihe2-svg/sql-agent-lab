"""Offline regression against saved replies; no clients, database or gold answers."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from .dataset import Question
from .review_contract import (CHECKS, ReviewProtocolError, contains_fragment,
                              descriptive_anchor, parse_report, validate_requirements)
from .review_quotes import quote_in_source
from .semantic_guard import apply_revision, RevisionRejected
from .output_requirements import freeze_requirements

def isolated_contract(row):
    q=Question(**row['question'])
    return freeze_requirements(json.dumps({'status':'clear','requested':[{'id':'o1','source':'question','quote':q.question,'description':'Test-only logical output'}],'constraints':[],'ambiguities':[]}),q)


FIXTURE = Path(__file__).with_name('fixtures') / 'semantic-review-deepseek-core6-real.json'
ROWS = json.loads(FIXTURE.read_text(encoding='utf8'))


@pytest.mark.parametrize('row', ROWS, ids=lambda r: r['question']['question_id'])
def test_real_replies_validate_without_word_coverage_or_false_source_failures(row):
    original = copy.deepcopy(row)
    report = parse_report(row['raw'])
    result = validate_requirements(report, Question(**row['question']), row['before'], row['sources'])
    assert result['status'] in ('validated', 'validated_with_warnings')
    assert result['semantic_completeness'] == 'not_verified'
    qid = row['question']['question_id']
    codes = {w['code'] for w in result['warnings']}
    if qid == '129':
        assert not codes  # Narrative words need not be quoted.
    elif qid == '333':
        assert codes == {'document_source_recovered'}
        recovered = [c for c in result['citations'] if c['kind'] == 'supporting_evidence']
        assert len(recovered) == 3
        assert recovered[0]['sources'] == ['d5', 'd6']  # No arbitrary unique-column inference.
    elif qid == '203':
        assert codes == {'abbreviated_sql_evidence'}
    assert row == original  # Raw responses, SQL and source labels stay intact.


@pytest.mark.parametrize('quote,source,expected', [
    ('first\n month', 'the first   month of operation', True),
    ('bank’s first month', "bank's first month", True),
    ('5 badges', '50 badges', False),
    ('1980', '19800', False),
    ('before 1980', 'before 1981', False),
    ('count people', 'count badges', False),
    ('not before 1980', 'before 1980', False),
    ('Germany', 'germany', False),
])
def test_only_presentation_differences_are_normalized(quote, source, expected):
    assert quote_in_source(quote, source) is expected


def test_fabricated_source_cannot_be_recovered_from_other_documents():
    row = ROWS[4]
    report = parse_report(row['raw'])
    report['requirements'][3]['quote'] = 'the id of an invented customer'
    with pytest.raises(ReviewProtocolError) as error:
        validate_requirements(report, Question(**row['question']), row['before'], row['sources'])
    assert error.value.code == 'ungrounded_requirement'


@pytest.mark.parametrize('fragment', [
    "SELECT id ... WHERE n = 5", "WHERE n = 5 ... SELECT name",
    "SELECT name ... WHERE n != 5", "SELECT name ... WHERE label = '...bad'",
])
def test_ellipses_cannot_invent_or_reorder_sql(fragment):
    assert not descriptive_anchor("SELECT name FROM items WHERE n = 5 AND label = '...ok'", fragment)


def test_abbreviated_evidence_is_never_an_edit_anchor():
    before = 'SELECT name FROM items WHERE n = 5'
    fragment = 'SELECT name ... WHERE n = 5'
    assert descriptive_anchor(before, fragment)
    assert not contains_fragment(before, fragment)
    q = Question('unrelated', 'db', 'Return names for n = 6')
    report = {'checks': {k: {'status': 'fail' if k == 'filters' else 'pass'} for k in CHECKS},
              'changes': [{'checks': ['filters'], 'requirement_quote': 'n = 6',
                           'before': fragment, 'after': 'SELECT name FROM items WHERE n = 6',
                           'evidence': [{'source': 'question', 'quote': 'n = 6'}]}]}
    with pytest.raises(RevisionRejected) as error:
        apply_revision(before, report, q, {'question': q.question})
    assert error.value.code == 'ambiguous_fragment'


def test_revision_report_requires_exact_sql_fragments_even_for_passed_items():
    row = ROWS[5]
    report = parse_report(row['raw'])
    report['action'] = 'revise'
    with pytest.raises(ReviewProtocolError) as error:
        validate_requirements(report, Question(**row['question']), row['before'], row['sources'])
    assert error.value.code == 'unmatched_sql_anchor'


def test_reference_quotes_normalize_without_changing_the_patch():
    before = 'SELECT n FROM items WHERE n = 5'
    q = Question('unrelated', 'db', "Use the bank's\n preferred threshold of 6")
    report = {'checks': {'filters': {'status': 'fail'}}, 'changes': [
        {'checks': ['filters'], 'requirement_quote': 'preferred threshold of 6',
         'evidence': [{'source': 'question', 'quote': 'bank’s preferred threshold of 6'}],
         'before': 'n = 5', 'after': 'n = 6'}]}
    assert apply_revision(before, report, q, {'question': q.question}) == 'SELECT n FROM items WHERE n = 6'


@pytest.mark.parametrize('row', ROWS, ids=lambda r: r['question']['question_id'])
def test_review_stage_replays_saved_reply_and_keeps_sql(row, monkeypatch):
    from . import semantic_review
    sources = {'question': row['question']['question'], 'evidence': row['question']['evidence'], **row['sources']}
    monkeypatch.setattr(semantic_review, 'evidence_context', lambda *args: ({}, sources, []))
    events = []
    class SavedReplyAgent:
        config = SimpleNamespace(semantic_output_tokens=1600, tools=SimpleNamespace(
            diagnose_sql_readonly=lambda *args: {'status': 'completed', 'findings': []}))
        def _event(self, state, event, **fields): events.append({'event': event, **fields})
        def _fit(self, role, prompt, schema): return [{'content': prompt(json.dumps(schema))}], {}
        def _ask_reference(self, state, role, messages, budget): return row['raw']
    state = {'question': Question(**row['question']), 'sql': row['before'], 'full_schema': {}, 'feedback': {'status': 'ok'}}
    result = semantic_review._review_sql(SavedReplyAgent(), state, isolated_contract(row))
    assert result.get('sql', state['sql']) == row['before']
    expected = 'output_unverified'  # Old replies have no per-column output contract.
    assert result['semantic_status'] == expected, result
    assert not any(e['event'] == 'semantic_review_error' for e in events)
    assert result['semantic_metrics']['sql_changed'] is False


@pytest.mark.parametrize('error,kind', [(ReviewProtocolError('invalid_json', 'bad report'), 'report_validation'),
                                      (TimeoutError('local simulated timeout'), 'review_runtime')])
def test_report_errors_and_runtime_errors_are_distinct(error, kind, monkeypatch):
    from . import semantic_review
    row = ROWS[0]
    monkeypatch.setattr(semantic_review, 'evidence_context', lambda *args: ({}, {}, []))
    events = []
    class Agent:
        config = SimpleNamespace(semantic_output_tokens=1600, tools=SimpleNamespace(
            diagnose_sql_readonly=lambda *args: {'status': 'completed', 'findings': []}))
        def _event(self, state, event, **fields): events.append({'event': event, **fields})
        def _fit(self, *args): raise error
    result = semantic_review._review_sql(Agent(), {'question': Question(**row['question']),
        'sql': row['before'], 'full_schema': {}, 'feedback': {'status': 'ok'}}, isolated_contract(row))
    assert result['semantic_report']['failure_kind'] == kind
    assert next(e for e in events if e['event'] == 'semantic_review_error')['failure_kind'] == kind
    assert result['semantic_metrics']['sql_changed'] is False
