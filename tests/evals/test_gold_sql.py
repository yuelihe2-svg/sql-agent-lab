from collections import defaultdict
from pathlib import Path
import json
import sqlite3

from sql_agent_lab.macsql.tools import SQLiteTools

from evals.graders.sql_grader import fetch_rows
from evals.shopbench_v1.catalog import materialize


ROOT = Path(__file__).resolve().parents[2]


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_materialized_cases_hide_gold_sql(tmp_path):
    materialize()
    rules = (ROOT / "evals" / "shopbench_v1" / "business_rules.md").read_text(encoding="utf-8")
    case_text = "\n".join((ROOT / "evals" / "cases").joinpath(name).read_text(encoding="utf-8")
                          for name in ("single_turn.jsonl", "multi_turn.jsonl"))
    gold = _rows(ROOT / "evals" / "gold" / "single_turn_gold.jsonl")
    multi = _rows(ROOT / "evals" / "gold" / "multi_turn_gold.jsonl")
    assert len(gold) == 40
    assert len(multi) == 80
    assert sum(1 for row in multi if row["status"] == "not_implemented") == 1
    assert sum(1 for row in multi if row["status"] == "eligible") == 79
    for row in gold + [item for item in multi if item["sql"]]:
        assert row["sql"] not in case_text
        assert row["sql"] not in rules
    sessions = _rows(ROOT / "evals" / "cases" / "multi_turn.jsonl")
    assert len(sessions) == 20
    assert all(len(session["turns"]) == 4 for session in sessions)


def test_every_gold_sql_executes(shopbench):
    directory, _manifest = shopbench
    tools = SQLiteTools({name: directory / f"{name}.sqlite" for name in ("shop_a", "shop_b", "shop_edge")})
    materialize()
    failures = []
    for row in _rows(ROOT / "evals" / "gold" / "single_turn_gold.jsonl"):
        result = fetch_rows(tools, row["source_id"], row["sql"])
        if result["status"] != "ok" or not result["columns"]:
            failures.append((row["case_id"], result))
    for row in _rows(ROOT / "evals" / "gold" / "multi_turn_gold.jsonl"):
        if row["status"] != "eligible":
            assert row["sql"] is None
            continue
        result = fetch_rows(tools, row["source_id"], row["sql"])
        if result["status"] != "ok" or not result["columns"]:
            failures.append((row["turn_id"], result))
    assert failures == []


def test_money_gold_matches_independent_cash_calculation(shopbench):
    directory, _manifest = shopbench
    conn = sqlite3.connect(directory / "shop_a.sqlite")
    tools = SQLiteTools({"shop_a": directory / "shop_a.sqlite"})
    materialize()
    gold = {row["case_id"]: row for row in _rows(ROOT / "evals" / "gold" / "single_turn_gold.jsonl")}

    def scalar(case_id):
        result = fetch_rows(tools, "shop_a", gold[case_id]["sql"])
        assert result["status"] == "ok"
        assert len(result["rows"]) == 1
        return result["rows"][0][0]

    def cash(table, status, column, start, end):
        return conn.execute(
            f"SELECT COALESCE(SUM(amount_cents), 0) FROM {table} "
            f"WHERE status = ? AND {column} >= ? AND {column} < ?",
            (status, start, end),
        ).fetchone()[0]

    october_captured = cash("payments", "succeeded", "paid_at", "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    october_refunded = cash("refunds", "completed", "completed_at", "2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
    assert scalar("S01") == conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    assert scalar("S14") == october_captured
    assert scalar("S15") == october_refunded
    assert scalar("S16") == october_captured - october_refunded
    assert scalar("S37") == 0
    assert isinstance(scalar("S37"), int)

    captured = defaultdict(int)
    refunded = defaultdict(int)
    for order_id, amount in conn.execute("SELECT order_id, amount_cents FROM payments WHERE status = 'succeeded'"):
        captured[order_id] += amount
    for order_id, amount in conn.execute(
        """
        SELECT p.order_id, r.amount_cents
        FROM refunds r JOIN payments p ON p.payment_id = r.payment_id
        WHERE r.status = 'completed'
        """
    ):
        refunded[order_id] += amount
    partial = sum(1 for order_id, amount in captured.items() if 0 < refunded[order_id] < amount)
    full = sum(1 for order_id, amount in captured.items() if amount > 0 and refunded[order_id] == amount)
    assert scalar("S35") == partial
    assert scalar("S36") == full
    conn.close()
