import json
import sqlite3
from pathlib import Path

from sql_agent_lab.macsql.dataset import Reference, score_sql
from sql_agent_lab.macsql.evaluation_audit import SCORER as BIRD_SCORER
from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits

from evals.graders.answer_grader import grade_answer
from evals.graders.sql_grader import SCORER, fetch_rows, grade_sql


ROOT = Path(__file__).resolve().parents[2]


def _tiny(tmp_path, schema, rows):
    path = tmp_path / "tiny.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    conn.executemany("INSERT INTO t VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return SQLiteTools({"tiny": path})


def test_shopbench_scorer_is_separate_from_bird(tmp_path):
    path = tmp_path / "bird.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t(n INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    tools = SQLiteTools({"tiny": path})
    assert BIRD_SCORER == "complete_row_set_v1_ignore_order_and_duplicates"
    assert SCORER == "shopbench_strict_v1"
    gold = "SELECT n FROM t UNION ALL SELECT n FROM t"
    assert score_sql(tools, Reference("tiny", gold), "SELECT n FROM t") == 1.0
    graded = grade_sql(tools, "tiny", gold, "SELECT n FROM t")
    assert graded["sql_correct"] is False


def test_order_null_duplicate_empty_and_text_are_distinct(tmp_path):
    tools = _tiny(tmp_path, "CREATE TABLE t(label TEXT, n INTEGER)", [("b", 2), ("a", 1), ("a", None)])
    ordered = grade_sql(
        tools, "tiny",
        "SELECT label FROM t ORDER BY label ASC",
        "SELECT label FROM t ORDER BY label DESC",
        order_sensitive=True,
    )
    unordered = grade_sql(
        tools, "tiny",
        "SELECT label FROM t ORDER BY label ASC",
        "SELECT label FROM t ORDER BY label DESC",
        order_sensitive=False,
    )
    assert ordered["sql_correct"] is False
    assert unordered["sql_correct"] is True
    duplicates = grade_sql(tools, "tiny", "SELECT label FROM t WHERE label = 'a'", "SELECT DISTINCT label FROM t WHERE label = 'a'")
    assert duplicates["sql_correct"] is False
    empty = grade_sql(tools, "tiny", "SELECT label FROM t WHERE label = 'missing'", "SELECT label FROM t WHERE n = 999")
    assert empty["sql_correct"] is True
    nulls = grade_sql(tools, "tiny", "SELECT n FROM t WHERE label = 'a' AND n IS NULL", "SELECT n FROM t WHERE label = 'a' AND n IS NULL")
    assert nulls["sql_correct"] is True
    coerced = grade_sql(tools, "tiny", "SELECT '0012'", "SELECT 12")
    assert coerced["sql_correct"] is False
    money = grade_sql(tools, "tiny", "SELECT 10000", "SELECT 10001")
    assert money["sql_correct"] is False


def test_gold_failure_is_not_a_wrong_answer(tmp_path):
    tools = _tiny(tmp_path, "CREATE TABLE t(label TEXT, n INTEGER)", [("a", 1)])
    failed_gold = grade_sql(tools, "tiny", "SELECT missing FROM t", "SELECT n FROM t")
    assert failed_gold["status"] == "evaluation_error"
    assert failed_gold["sql_correct"] is None
    failed_candidate = grade_sql(tools, "tiny", "SELECT n FROM t", "SELECT missing FROM t")
    assert failed_candidate["status"] == "scored"
    assert failed_candidate["sql_correct"] is False


def test_adversarial_sql_is_rejected(shopbench):
    directory, _manifest = shopbench
    tools = SQLiteTools({"shop_a": directory / "shop_a.sqlite"})
    gold = {row["case_id"]: row for row in (
        json.loads(line) for line in (ROOT / "evals" / "gold" / "single_turn_gold.jsonl").read_text(encoding="utf-8").splitlines()
    )}
    probes = [
        json.loads(line)
        for line in (ROOT / "evals" / "fixtures" / "predictions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {probe["probe_id"] for probe in probes} >= {
        "P-correct-S16", "P-join-multiplication", "P-wrong-date",
        "P-missing-payment-status", "P-missing-refund-status", "P-missing-distinct",
    }
    for probe in probes:
        expected = gold[probe["case_id"]]
        graded = grade_sql(
            tools, "shop_a", expected["sql"], probe["predicted_sql"],
            order_sensitive=expected["order_sensitive"], numeric_tolerance=expected["numeric_tolerance"],
        )
        assert graded["sql_correct"] is probe["expect_sql_correct"], probe["probe_id"]


def test_answer_can_be_wrong_when_sql_is_right(shopbench):
    directory, _manifest = shopbench
    tools = SQLiteTools({"shop_a": directory / "shop_a.sqlite"})
    gold_sql = next(
        json.loads(line)["sql"]
        for line in (ROOT / "evals" / "gold" / "single_turn_gold.jsonl").read_text(encoding="utf-8").splitlines()
        if '"S16"' in line
    )
    execution = fetch_rows(tools, "shop_a", gold_sql)
    answer = grade_answer(
        gold=execution,
        execution=execution,
        answer={"columns": ["net_receipts_cents"], "rows": [[1]], "text": "净收款额是 1 分"},
        question="2026年10月净收款额是多少？",
    )
    assert answer["answer_correct"] is False
    assert answer["faithfulness"] is False


def test_database_change_during_grading_is_an_evaluation_error(shopbench, tmp_path):
    source = shopbench[0] / "shop_edge.sqlite"
    target = tmp_path / "shop_edge.sqlite"
    target.write_bytes(source.read_bytes())
    tools = SQLiteTools({"shop_edge": target})

    def mutate():
        conn = sqlite3.connect(target)
        conn.execute("INSERT INTO categories(category_id, category_name) VALUES (99, '临时')")
        conn.commit()
        conn.close()

    graded = grade_sql(tools, "shop_edge", "SELECT COUNT(*) AS n FROM categories", "SELECT COUNT(*) AS n FROM categories",
                       after_gold=mutate)
    assert graded["status"] == "evaluation_error"
    assert graded["reason"] == "database_changed"
    assert graded["sql_correct"] is None


def test_preview_limit_does_not_score_the_full_result(shopbench):
    path = shopbench[0] / "shop_a.sqlite"
    tools = SQLiteTools({"shop_a": path}, ToolLimits(preview_rows=5))
    preview = tools.execute_sql_readonly("shop_a", "SELECT customer_id FROM customers ORDER BY customer_id")
    full = fetch_rows(tools, "shop_a", "SELECT customer_id FROM customers ORDER BY customer_id")
    assert preview["preview_row_count"] == 5
    assert len(full["rows"]) == 120
    graded = grade_sql(
        tools, "shop_a",
        "SELECT customer_id FROM customers ORDER BY customer_id",
        "SELECT customer_id FROM customers ORDER BY customer_id",
        order_sensitive=True,
    )
    assert graded["sql_correct"] is True
    assert graded["row_count"] == 120
