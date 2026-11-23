"""Offline workspace acceptance using wrong-but-executable SQL and a SQLite oracle."""
from contextlib import closing
from dataclasses import replace
import sqlite3

import pytest

from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent
from sql_agent_lab.macsql.reference_core import ModelReply
from test_conversations_api import build, wait_done


PAID = "统计2024年已付款订单总金额。已付款指 status = paid。"
TOTAL = "SELECT SUM(amount) FROM orders WHERE year=2024 AND status='paid'"
CATEGORY = "按商品分类统计订单数"
COUNTS = ("SELECT i.category, COUNT(DISTINCT o.id) FROM orders o "
          "JOIN order_items i ON i.order_id=o.id GROUP BY i.category")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    path = app.databases["shop"]
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            CREATE TABLE orders (id INTEGER PRIMARY KEY, year INTEGER, status TEXT, amount INTEGER);
            INSERT INTO orders VALUES (1, 2024, 'paid', 100), (2, 2024, 'paid', 200),
                (3, 2024, 'cancelled', 900), (4, 2023, 'paid', 700);
            CREATE TABLE order_items (order_id INTEGER REFERENCES orders(id), category TEXT);
            INSERT INTO order_items VALUES (1, 'books'), (1, 'books'), (2, 'books'),
                (2, 'gifts'), (2, 'gifts');
        """)
    return app, path


def ask(app, cid, question):
    app.worker_done.clear()
    app.post_conversation_message(cid, {"question": question})
    wait_done(app)
    return app.conversation_detail(cid)["runs"][-1]["result"]


@pytest.mark.parametrize("question, oracle, candidate, expected, display", [
    (PAID, TOTAL, TOTAL, [(300,)], True),
    (PAID, TOTAL, "SELECT SUM(amount) FROM orders WHERE status='paid'", [(300,)], False),
    (PAID, TOTAL, "SELECT SUM(amount) FROM orders WHERE year=2024", [(300,)], False),
    (PAID, TOTAL, TOTAL.replace(" AND ", " OR "), [(300,)], False),
    (PAID, TOTAL, TOTAL.replace("SUM(amount)", "COUNT(*)"), [(300,)], False),
    (CATEGORY, COUNTS, COUNTS, [("books", 2), ("gifts", 1)], True),
    (CATEGORY, COUNTS, COUNTS.replace("DISTINCT o.id", "*"), [("books", 2), ("gifts", 1)], False),
    (CATEGORY, COUNTS, COUNTS.replace("i.order_id=o.id", "o.id=o.id"), [("books", 2), ("gifts", 1)], False),
])
def test_initial_query_against_sqlite(workspace, monkeypatch, question, oracle, candidate, expected, display):
    app, path = workspace
    with closing(sqlite3.connect(path)) as conn:
        assert sorted(conn.execute(oracle)) == sorted(expected)
        assert (sorted(conn.execute(candidate)) == sorted(expected)) == display
    calls = []
    def model(**kwargs):
        calls.append(kwargs["messages"])
        return ModelReply(chr(96)*3 + "sql\n" + candidate + "\n" + chr(96)*3, 1, 1, simulated=True)
    monkeypatch.setattr("sql_agent_lab.macsql.live_ui.ReferenceMacSQLAgent",
                        lambda models, config: ReferenceMacSQLAgent(models, config=replace(config, model=model)))
    cid = app.create_conversation({"datasource_id": "shop"})["id"]
    result = ask(app, cid, question)
    assert calls
    if display:
        assert sorted(tuple(row) for row in result["rows"]) == sorted(expected)
    else:
        assert result["needs_clarification"]
        assert result["sql"] is None
        assert result["rows"] == []


def test_sales_clarifies_before_model_then_accepts_full_question(workspace):
    app, _ = workspace
    calls = []
    def behavior(config, question):
        calls.append(question.question)
        return TOTAL
    app.behavior = behavior
    cid = app.create_conversation({"datasource_id": "shop"})["id"]
    result = ask(app, cid, "看看去年销售情况")
    assert result["needs_clarification"]
    assert all(metric in result["summary"] for metric in ("销售总金额", "订单数量", "销售趋势"))
    assert not calls
    result = ask(app, cid, PAID)
    assert result["rows"] == [[300]]
    assert len(calls) == 1
