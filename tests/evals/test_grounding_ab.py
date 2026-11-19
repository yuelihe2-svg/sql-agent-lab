"""The paired grounding set is auditable. Model scores are not invented."""
from contextlib import closing
import sqlite3

from sql_agent_lab.evals.grounding_ab import (
    CASES, build_database, comparison_report, retrieval_report, score_sql)
from sql_agent_lab.workspace.profile import extract_profile


def test_paired_set_measures_retrieval_and_leaves_model_metrics_unmeasured(tmp_path):
    database = build_database(tmp_path / 'shop.sqlite')
    profile = extract_profile(database)
    report = comparison_report(profile)
    again = retrieval_report(profile)
    assert 20 <= len(CASES) <= 30
    assert report['case_count'] == len(CASES)
    assert report['same_model'] is True
    assert report['same_repair_budget'] is True
    assert report['arms']['baseline']['use_grounding'] is False
    assert report['arms']['grounded']['use_grounding'] is True
    assert report['retrieval_hit_rate'] == again['retrieval_hit_rate']
    assert report['retrieval_precision'] == again['retrieval_precision']
    assert report['retrieval_recall'] == 1.0
    assert report['knowledge_hit_rate'] == 1.0
    assert report['retrieval_precision'] > 0.75
    assert report['cross_source_leaks'] == []
    assert report['model_run'] is False
    for name in ('execution_accuracy', 'sql_execution_success_rate', 'agent_retry_rate',
                 'end_to_end_latency', 'token_usage', 'estimated_api_cost'):
        assert report[name] == 'not_measured'
    correct = score_sql(database, 'SELECT COUNT(*) FROM customers', [[3]])
    wrong = score_sql(database, 'SELECT COUNT(*) FROM orders', [[3]])
    rejected = score_sql(database, "INSERT INTO notes VALUES (2, 'x')", [[1]])
    assert correct == {'execution_ok': True, 'accurate': True, 'reason': 'compared'}
    assert wrong['execution_ok'] is True and wrong['accurate'] is False
    assert rejected['execution_ok'] is False
    unordered = score_sql(database, 'SELECT name FROM customers', [['Cy'], ['Ada'], ['Bo']])
    ordered = score_sql(database, 'SELECT name FROM customers ORDER BY name DESC',
                        [['Ada'], ['Bo'], ['Cy']], ordered=True)
    swapped = score_sql(database, 'SELECT name FROM customers ORDER BY name DESC',
                        [['Cy'], ['Bo'], ['Ada']], ordered=True)
    duplicate = score_sql(database, "SELECT status FROM orders WHERE status = 'paid'", [['paid'], ['paid']])
    missing_duplicate = score_sql(database, "SELECT DISTINCT status FROM orders WHERE status = 'paid'", [['paid'], ['paid']])
    assert unordered['accurate'] is True
    assert ordered['accurate'] is False
    assert swapped['accurate'] is True
    assert duplicate['accurate'] is True
    assert missing_duplicate['accurate'] is False
    with closing_connection(database) as conn:
        conn.execute('CREATE TABLE marks(n REAL, note TEXT)')
        conn.execute("INSERT INTO marks VALUES (1.0000004, NULL)")
        conn.commit()
    assert score_sql(database, 'SELECT n, note FROM marks', [[1.0, None]])['accurate'] is True


def closing_connection(database):
    return closing(sqlite3.connect(database))


def test_harness_keeps_expected_rows_out_of_the_agent_input(tmp_path):
    from sql_agent_lab.evals.run_grounding_ab import case_input, summarize
    database = build_database(tmp_path / 'shop.sqlite')
    profile = extract_profile(database)
    case = next(item for item in CASES if item['id'] == 'customers-count')
    question, block, info = case_input(case, profile, 'baseline')
    assert question.question == case['question']
    assert block == ''
    assert info['retrieval_called'] is False
    _, grounded, on = case_input(case, profile, 'grounded')
    assert on['retrieval_called'] is True
    assert '[[3]]' not in grounded
    assert 'expected_rows' not in grounded
    summary = summarize([
        {'arm': 'baseline', 'question_id': 'a', 'scored': True, 'execution_correct': False,
         'execution_ok': True, 'total_latency_ms': 10, 'input_tokens': 5, 'output_tokens': 2,
         'refiner_called': True, 'repair_success': False, 'selector_invoked': False},
        {'arm': 'grounded', 'question_id': 'a', 'scored': True, 'execution_correct': True,
         'execution_ok': True, 'total_latency_ms': 12, 'input_tokens': 8, 'output_tokens': 3,
         'refiner_called': False, 'repair_success': False, 'selector_invoked': False},
    ])
    assert summary['wrong_to_correct'] == ['a']
    assert summary['correct_to_wrong'] == []
    assert summary['baseline_execution_accuracy'] == 0
    assert summary['grounded_execution_accuracy'] == 1
    assert summary['estimated_cost'] is None
