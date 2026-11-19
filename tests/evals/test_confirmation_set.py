"""The confirmation set is frozen and is not a model score."""
from sql_agent_lab.evals.confirmation_set import CONFIRMATION_CASES, confirmation_database
from sql_agent_lab.evals.grounding_ab import score_sql


AUDITOR = {
    'C01': 'SELECT COUNT(*) FROM members',
    'C06': 'SELECT m.name FROM loans l JOIN members m ON m.id = l.member_id',
    'C10': 'SELECT name FROM members ORDER BY name',
    'C14': 'SELECT status, SUM(fee) FROM loans GROUP BY status',
    'C26': 'SELECT m.name FROM members m LEFT JOIN loans l ON l.member_id = m.id WHERE l.id IS NULL',
    'C28': 'SELECT AVG(fee) FROM loans',
    'C34': 'SELECT COUNT(*) FROM loans WHERE return_note IS NULL',
}


def test_confirmation_set_is_frozen_and_auditable(tmp_path):
    ids = [item['id'] for item in CONFIRMATION_CASES]
    assert len(ids) == len(set(ids))
    assert 30 <= len(CONFIRMATION_CASES) <= 50
    database = confirmation_database(tmp_path / 'library.sqlite')
    for case_id, sql in AUDITOR.items():
        case = next(item for item in CONFIRMATION_CASES if item['id'] == case_id)
        grade = score_sql(database, sql, case['expected_rows'], ordered=bool(case.get('ordered')))
        assert grade['accurate'] is True, case_id
