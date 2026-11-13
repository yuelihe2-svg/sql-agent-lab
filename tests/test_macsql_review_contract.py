"""Protocol, requirement coverage and recorded-response regressions; no API."""
import copy
import json
from pathlib import Path
import pytest
from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql import semantic_guard
from sql_agent_lab.macsql.semantic_guard import apply_revision, RevisionRejected
from sql_agent_lab.macsql.review_contract import (CHECKS, parse_report, ReviewProtocolError,
                                                       validate_requirements, check_revision_requirements)

Q = Question('independent', 'library', "Return names from Cedar Press with genre 'History'.")
SQL = "SELECT title FROM books WHERE genre = 'History'"


def payload():
    checks = {k: {'status': 'pass', 'reason': 'not required'} for k in CHECKS}
    checks['filters'] = {'status': 'fail', 'reason': 'Missing publisher condition'}
    return {'action': 'revise', 'checks': checks, 'requirements': [
        {'id': 'r1', 'quote': 'Return names', 'check': 'output', 'status': 'pass',
         'sql_fragment': 'SELECT title', 'reason': 'names requested'},
        {'id': 'r2', 'quote': 'from Cedar Press', 'check': 'filters', 'status': 'fail',
         'sql_fragment': '', 'reason': 'publisher absent'},
        {'id': 'r3', 'quote': "with genre 'History'.", 'check': 'filters', 'status': 'pass',
         'sql_fragment': "genre = 'History'", 'reason': 'genre present'}],
        'changes': [{'checks': ['filters'], 'requirements': ['r2'],
                     'before': "WHERE genre = 'History'",
                     'after': "WHERE genre = 'History' AND publisher = 'Cedar Press'",
                     'requirement_quote': 'from Cedar Press',
                     'evidence': [{'source': 'question', 'quote': 'from Cedar Press'}]}]}


@pytest.mark.parametrize('label', ['name', 'check'])
def test_named_lists_and_mappings_are_equivalent(label):
    p = payload()
    p['checks'] = [{label: k, **v} for k, v in p['checks'].items()]
    parsed = parse_report('```json\n' + json.dumps(p) + '\n```')
    assert parsed['checks'] == payload()['checks']
    assert parsed['format_normalized'] == 'checks_list_to_mapping'


@pytest.mark.parametrize('mode', ['missing', 'duplicate', 'conflicting_label', 'unknown'])
def test_compatibility_does_not_invent_missing_or_conflicting_checks(mode):
    p = payload(); p['checks'] = [{'name': k, **v} for k, v in p['checks'].items()]
    if mode == 'missing': p['checks'].pop()
    elif mode == 'duplicate': p['checks'].append(p['checks'][0])
    elif mode == 'conflicting_label': p['checks'][0]['check'] = 'joins'
    else: p['checks'][0]['name'] = 'invented'
    with pytest.raises(ReviewProtocolError): parse_report(json.dumps(p))


def test_duplicate_json_keys_are_not_silently_overwritten():
    with pytest.raises(ReviewProtocolError, match='Duplicate JSON key'):
        parse_report('{"action":"revise","action":"accept"}')


def test_atomic_requirements_cover_text_and_preserve_passed_sql():
    p = payload()
    assert validate_requirements(p, Q, SQL)['status'] == 'validated'
    candidate = apply_revision(SQL, p, Q, {'question': Q.question})
    check_revision_requirements(SQL, candidate, p)
    assert "publisher = 'Cedar Press'" in candidate


def test_text_coverage_is_not_claimed_as_semantic_completeness():
    p = payload(); p['requirements'].pop(1)
    p['action'] = 'accept'; p['changes'] = []; p['checks']['filters']['status'] = 'pass'
    # A rule cannot infer a complete semantic checklist from raw word coverage.
    result = validate_requirements(p, Q, SQL)
    assert result['semantic_completeness'] == 'not_verified'


@pytest.mark.parametrize('mode,code', [
    ('missing', 'missing_requirements'), ('anchor', 'unmatched_sql_anchor'),
    ('empty_anchor', 'missing_sql_anchor'), ('quote', 'ungrounded_requirement'),
    ('duplicate', 'duplicate_requirement'), ('contradiction', 'decision_conflict')])
def test_unsubstantiated_requirement_claims_fail_closed(mode, code):
    p = payload()
    if mode == 'missing': p.pop('requirements')
    elif mode == 'anchor': p['requirements'][0]['sql_fragment'] = 'SELECT invented'
    elif mode == 'empty_anchor': p['requirements'][0]['sql_fragment'] = ''
    elif mode == 'quote': p['requirements'][0]['quote'] = 'Return amounts'
    elif mode == 'duplicate': p['requirements'].append(copy.deepcopy(p['requirements'][0]))
    else: p['action'] = 'accept'
    with pytest.raises(ReviewProtocolError) as error: validate_requirements(p, Q, SQL)
    assert error.value.code == code


def test_uncited_evidence_is_visible_without_a_word_coverage_failure():
    p = payload(); q = Question('other', Q.db_id, Q.question, 'Exclude years before 1980.')
    result = validate_requirements(p, q, SQL)
    assert result['status'] == 'validated_with_warnings'
    assert result['warnings'][0]['code'] == 'uncited_evidence'


@pytest.mark.parametrize('mode,code', [('unlinked', 'invalid_requirement_mapping'),
                                     ('remove_passed', 'passing_requirement_changed'),
                                     ('add_output', 'output_shape_changed')])
def test_revision_must_keep_passed_requirements_and_output_shape(mode, code):
    p = payload(); candidate = SQL + " AND publisher = 'Cedar Press'"
    if mode == 'unlinked': p['changes'][0].pop('requirements')
    elif mode == 'remove_passed': candidate = "SELECT title FROM books WHERE publisher = 'Cedar Press'"
    else: candidate = candidate.replace('SELECT title', 'SELECT title, rating')
    with pytest.raises(RevisionRejected) as error: check_revision_requirements(SQL, candidate, p)
    assert error.value.code == code


def test_recorded_v2_outputs_normalize_but_do_not_bypass_guards():
    path = Path(semantic_guard.__file__).parent / 'fixtures/semantic-review-v2-real.json'
    rows = json.loads(path.read_text(encoding='utf8'))
    assert len(rows) == 7
    for row in rows:
        q = Question(**row['question'])
        if q.question_id == '165':
            with pytest.raises(ReviewProtocolError, match='all six'): parse_report(row['raw'])
            continue
        parsed = parse_report(row['raw'])
        if q.question_id in ('2', '308'):
            assert parsed['format_normalized'] == 'checks_list_to_mapping'
        # Old reviews lack atomic requirements: compatibility is not a semantic pass.
        with pytest.raises(ReviewProtocolError) as error: validate_requirements(parsed, q, row['before'])
        assert error.value.code == 'missing_requirements'
        if q.question_id == '129':
            with pytest.raises(RevisionRejected) as failure:
                apply_revision(row['before'], parsed, q, {'question': q.question,
                                                        'evidence': q.evidence, 'd14': "user's location"})
            assert failure.value.code == 'no_effect'


def test_recorded_v3_outputs_cannot_bypass_conflicts_or_edit_mapping():
    path = Path(semantic_guard.__file__).parent / 'fixtures/semantic-review-v3-real.json'
    for row in json.loads(path.read_text(encoding='utf8')):
        q = Question(**row['question'])
        with pytest.raises((ReviewProtocolError, RevisionRejected)) as failure:
            report = parse_report(row['raw'])
            validate_requirements(report, q, row['before'])
            apply_revision(row['before'], report, q, {'question':q.question, 'evidence':q.evidence})
        assert failure.value.code == row['expected_error']
