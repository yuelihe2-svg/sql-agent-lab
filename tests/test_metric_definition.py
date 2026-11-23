from contextlib import closing
import json
import sqlite3

import pytest

from sql_agent_lab.workspace.metric_definition import PAID_TOTAL_TITLE
from test_conversations_api import build, wait_done

QUESTION = "统计2024年已付款订单总金额"
SHOP = dict(table="orders", amount_column="amount", year_column="year",
            status_column="status", paid_value="paid")
ARCHIVE = dict(table="sales", amount_column="gross", year_column="fiscal_year",
               status_column="payment_code", paid_value=1)


def payload(data):
    return {"title": PAID_TOTAL_TITLE, "format": "json", "body": json.dumps(data)}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    for source, ddl, insert, rows in [
        ("shop", "CREATE TABLE orders(year INTEGER, status TEXT, amount INTEGER)",
         "INSERT INTO orders VALUES (?, ?, ?)",
         [(2024, "paid", 100), (2024, "paid", 200), (2024, "cancelled", 900), (2023, "paid", 700)]),
        ("archive", "CREATE TABLE sales(fiscal_year INTEGER, payment_code INTEGER, gross INTEGER)",
         "INSERT INTO sales VALUES (?, ?, ?)",
         [(2024, 1, 1100), (2024, 1, 1200), (2024, 0, 1900), (2023, 1, 1700)]),
    ]:
        with closing(sqlite3.connect(app.databases[source])) as conn:
            conn.execute(ddl)
            conn.executemany(insert, rows)
            conn.commit()
    def unavailable(*args, **kwargs):
        pytest.fail("A configured metric must never initialize a model")
    monkeypatch.setattr("sql_agent_lab.macsql.live_ui.LocalOllamaModel", unavailable)
    return app


def ask(app, source, question=QUESTION, *, cid=None):
    if cid is None:
        cid = app.create_conversation({"datasource_id": source})["id"]
    app.post_conversation_message(cid, {"question": question})
    wait_done(app)
    app.worker_done.clear()
    return app.conversation_detail(cid)["runs"][-1]["result"]


def test_metric_definition_is_used_only_for_its_source(workspace):
    app = workspace
    app.add_source_knowledge("shop", payload(SHOP))
    assert ask(app, "shop")["rows"] == [[300]]
    archive_cid = app.create_conversation({"datasource_id": "archive"})["id"]
    missing = ask(app, "archive", cid=archive_cid)
    assert missing["needs_clarification"] and missing["sql"] is None
    app.add_source_knowledge("archive", payload(ARCHIVE))
    archive = ask(app, "archive", cid=archive_cid)
    assert archive["rows"] == [[2300]]
    assert "payment_code" in archive["sql"] and "fiscal_year" in archive["sql"]
    assert ask(app, "archive", "统计2023年已付款订单总金额")["rows"] == [[1700]]
    assert ask(app, "shop")["rows"] == [[300]]


def test_invalid_duplicate_and_removed_definitions(workspace):
    app = workspace
    with pytest.raises(ValueError, match="找不到"):
        app.add_source_knowledge("shop", payload({**SHOP, "year_column": "fiscal_year"}))
    with pytest.raises(ValueError, match="非空文本或整数"):
        app.add_source_knowledge("shop", payload({**SHOP, "paid_value": True}))
    first = app.add_source_knowledge("shop", payload(SHOP))
    second = app.add_source_knowledge("shop", payload({**SHOP, "paid_value": "cancelled"}))
    assert ask(app, "shop")["needs_clarification"]
    app.delete_source_knowledge("shop", first["id"])
    assert ask(app, "shop")["rows"] == [[900]]
    app.delete_source_knowledge("shop", second["id"])
    assert ask(app, "shop")["needs_clarification"]
