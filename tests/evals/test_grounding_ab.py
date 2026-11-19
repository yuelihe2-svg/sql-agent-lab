"""The paired grounding set is auditable. Model scores are not invented."""
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
    assert report['retrieval_recall'] == again['retrieval_recall']
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
