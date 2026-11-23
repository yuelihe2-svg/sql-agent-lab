"""Keyword output stays on the frozen baseline. Other strategies are measured apart from it."""
import json
from pathlib import Path
import tempfile

from sql_agent_lab.evals.grounding_ab import build_database
from sql_agent_lab.evals.retrieval_strategies import (
    keyword_snapshot, load_keyword_baseline, probe_report, strategy_report,
)
from sql_agent_lab.workspace.profile import extract_profile


def _profile():
    root = Path(tempfile.mkdtemp()) / 'shop.sqlite'
    build_database(root)
    return extract_profile(root)


def test_keyword_baseline_matches_the_frozen_file():
    profile = _profile()
    fresh = keyword_snapshot(profile)
    frozen = load_keyword_baseline()
    assert fresh == frozen
    report = strategy_report(profile)
    assert report['questions']['keyword'] == frozen
    keyword = report['strategies']['keyword']
    assert keyword['schema_recall'] == 1.0
    assert keyword['schema_hit_rate'] == 1.0
    assert keyword['knowledge_labeled_cases'] == 3
    for name in ('bm25', 'hybrid'):
        other = report['strategies'][name]
        assert other['schema_precision'] == keyword['schema_precision']
        assert other['schema_recall'] == 1.0
        assert other['irrelevant_schema_hits'] == keyword['irrelevant_schema_hits']
    assert report['strategies']['hybrid']['mean_context_chars'] > keyword['mean_context_chars']
    assert json.dumps(report).count('combined_score') == 0


def test_knowledge_probes_do_not_treat_lexical_misses_as_semantic_search():
    rows = {item['id']: item for item in probe_report(_profile())}
    schema_word = rows['schema-word-only']['strategies']
    assert schema_word['keyword']['knowledge_ids'] == []
    assert schema_word['bm25']['knowledge_ids'] == ['order-def']
    assert schema_word['bm25']['forbidden_returned'] == []
    assert rows['schema-word-only']['strategies']['hybrid']['one_sided_relationships'] > 0
    synonym = rows['synonym-gap']
    assert synonym['embedding_candidate'] is True
    assert synonym['strategies']['keyword']['hit'] is False
    assert synonym['strategies']['bm25']['hit'] is False
    assert rows['cross-source']['strategies']['bm25']['forbidden_returned'] == []
    assert rows['cross-source']['strategies']['bm25']['knowledge_ids'] == ['shop-revenue']
    assert rows['title-rank']['strategies']['bm25']['knowledge_ids'][0] == 'title-paid'
    assert rows['title-rank']['strategies']['keyword']['knowledge_ids'][0] == 'body-paid'
    chinese = rows['chinese-unsegmented']
    assert chinese['chinese_semantic_search'] is False
    assert chinese['strategies']['keyword']['knowledge_ids'] == ['active-zh']
    assert chinese['strategies']['bm25']['knowledge_ids'] == []
    assert rows['chinese-exact']['strategies']['keyword']['hit'] is True
    assert rows['chinese-exact']['strategies']['bm25']['hit'] is True
