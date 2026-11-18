import json
from pathlib import Path

from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits

from evals.graders.sql_grader import fetch_rows
from evals.runner import model_input


ROOT = Path(__file__).resolve().parents[2]


def test_writes_and_unknown_databases_are_rejected(shopbench):
    directory, _manifest = shopbench
    path = directory / "shop_edge.sqlite"
    before = path.read_bytes()
    tools = SQLiteTools({"shop_edge": path, "shop_a": directory / "shop_a.sqlite", "shop_b": directory / "shop_b.sqlite"})
    for sql in ("DELETE FROM customers", "DROP TABLE customers", "SELECT 1; DELETE FROM customers"):
        assert tools.execute_sql_readonly("shop_edge", sql)["status"] == "error"
    assert path.read_bytes() == before
    try:
        tools.execute_sql_readonly("other-shop", "SELECT 1")
    except KeyError:
        rejected = True
    else:
        rejected = False
    assert rejected
    assert fetch_rows(tools, "missing", "SELECT 1")["reason"] == "unknown_source"


def test_same_table_names_do_not_share_rows(shopbench):
    directory, _manifest = shopbench
    tools = SQLiteTools({"shop_a": directory / "shop_a.sqlite", "shop_b": directory / "shop_b.sqlite"})
    left = fetch_rows(tools, "shop_a", "SELECT COUNT(*) AS customer_count FROM customers")
    right = fetch_rows(tools, "shop_b", "SELECT COUNT(*) AS customer_count FROM customers")
    assert left["rows"] != right["rows"]
    assert left["rows"] == [(120,)]
    assert right["rows"] == [(96,)]


def test_timeout_is_reported_and_paths_are_not_model_input(shopbench):
    directory, _manifest = shopbench
    path = directory / "shop_edge.sqlite"
    slow = SQLiteTools({"shop_edge": path}, ToolLimits(timeout_seconds=0.05))
    result = fetch_rows(
        slow, "shop_edge",
        "WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq LIMIT 5000000) SELECT MAX(x) FROM seq",
    )
    assert result["status"] == "timeout"
    case = json.loads((ROOT / "evals" / "cases" / "single_turn.jsonl").read_text(encoding="utf-8").splitlines()[0])
    payload = json.dumps(model_input(case), ensure_ascii=False)
    assert str(path) not in payload
    assert "WITH captured" not in payload
    safety = (ROOT / "evals" / "cases" / "safety.jsonl").read_text(encoding="utf-8")
    assert '"SAFE-TENANT"' in safety
    assert "multi_tenant_auth" in safety
    assert "not_implemented" in safety


def test_missing_reference_is_not_scored_as_zero():
    from evals.graders.sql_grader import grade_case
    graded = grade_case(None, "shop_a", None, "SELECT 1")
    assert graded["status"] == "skipped"
    assert graded["sql_correct"] is None
    assert graded["reason"] == "no_reference"
