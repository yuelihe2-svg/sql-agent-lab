import json
from pathlib import Path

from sql_agent_lab.macsql.tools import SQLiteTools

from evals.graders.semantic_execution import SCORER, grade_sql, question_requires_column_names
from evals.graders.sql_grader import SCORER as STRICT_SCORER
from evals.graders.sql_grader import grade_sql as grade_strict


ROOT = Path(__file__).resolve().parents[2]
S01_QUESTION = "数据库共有多少客户？"
S01_PREDICTED = "SELECT COUNT(*) FROM customers"


def _tools(tmp_path):
    import sqlite3
    path = tmp_path / "tiny.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(label TEXT, n INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [("a", 1), ("a", None), ("b", 2)])
    conn.commit()
    conn.close()
    return SQLiteTools({"tiny": path})


def test_alias_difference_is_execution_correct_when_values_match(tmp_path):
    tools = _tools(tmp_path)
    assert question_requires_column_names(S01_QUESTION) is False
    graded = grade_sql(
        tools, "tiny", "SELECT COUNT(*) AS customer_count FROM t", "SELECT COUNT(*) FROM t",
        question=S01_QUESTION,
    )
    strict = grade_strict(tools, "tiny", "SELECT COUNT(*) AS customer_count FROM t", "SELECT COUNT(*) FROM t")
    assert graded["scorer"] == "semantic_execution_v1"
    assert graded["sql_correct"] is True
    assert graded["column_names_checked"] is False
    assert strict["scorer"] == STRICT_SCORER == "shopbench_strict_v1"
    assert strict["sql_correct"] is False


def test_value_column_order_and_duplicate_mismatches_fail(tmp_path):
    tools = _tools(tmp_path)
    value = grade_sql(tools, "tiny", "SELECT 1 AS n", "SELECT 2 AS n", question=S01_QUESTION)
    order = grade_sql(
        tools, "tiny", "SELECT 1 AS left_value, 2 AS right_value", "SELECT 2 AS right_value, 1 AS left_value",
        question=S01_QUESTION,
    )
    duplicates = grade_sql(
        tools, "tiny",
        "SELECT label FROM t WHERE label = 'a'",
        "SELECT DISTINCT label FROM t WHERE label = 'a'",
        question=S01_QUESTION,
    )
    assert value["sql_correct"] is False and value["reason"] == "result_mismatch"
    assert order["sql_correct"] is False and order["reason"] == "column_order_mismatch"
    assert duplicates["sql_correct"] is False and duplicates["reason"] == "result_mismatch"


def test_required_names_types_nulls_and_row_order_stay_strict(tmp_path):
    tools = _tools(tmp_path)
    named = grade_sql(
        tools, "tiny", "SELECT 1 AS customer_count", "SELECT 1",
        question="请将输出列命名为 customer_count",
    )
    assert named["column_names_checked"] is True
    assert named["sql_correct"] is False
    assert named["reason"] == "column_name_mismatch"
    assert grade_sql(tools, "tiny", "SELECT '0012'", "SELECT 12")["sql_correct"] is False
    assert grade_sql(tools, "tiny", "SELECT NULL", "SELECT 0")["sql_correct"] is False
    ordered = grade_sql(
        tools, "tiny", "SELECT label FROM t ORDER BY label ASC", "SELECT label FROM t ORDER BY label DESC",
        order_sensitive=True,
    )
    unordered = grade_sql(
        tools, "tiny", "SELECT label FROM t ORDER BY label ASC", "SELECT label FROM t ORDER BY label DESC",
        order_sensitive=False,
    )
    assert ordered["sql_correct"] is False
    assert unordered["sql_correct"] is True


def test_saved_s01_prediction_passes_semantic_and_keeps_strict_failure(shopbench):
    directory, _manifest = shopbench
    tools = SQLiteTools({"shop_a": directory / "shop_a.sqlite"})
    gold = next(
        json.loads(line)
        for line in (ROOT / "evals" / "gold" / "single_turn_gold.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line)["case_id"] == "S01"
    )
    strict = grade_strict(
        tools, "shop_a", gold["sql"], S01_PREDICTED,
        order_sensitive=gold["order_sensitive"], numeric_tolerance=gold["numeric_tolerance"],
    )
    semantic = grade_sql(
        tools, "shop_a", gold["sql"], S01_PREDICTED,
        order_sensitive=gold["order_sensitive"], numeric_tolerance=gold["numeric_tolerance"],
        question=S01_QUESTION,
    )
    assert strict["sql_correct"] is False
    assert semantic["scorer"] == SCORER
    assert semantic["sql_correct"] is True
    assert semantic["reason"] == "match"
