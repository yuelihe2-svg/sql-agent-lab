"""Chinese workspace acceptance across independently scoped SQLite schemas."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from threading import Event

from sql_agent_lab.macsql import live_ui
from test_conversations_api import client_for
from test_metric_definition import payload


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "chinese_multisource.json").read_text(encoding="utf-8")
)


def test_multisource_acceptance(tmp_path, monkeypatch):
    sources = []
    for source, fixture in FIXTURE["sources"].items():
        path = tmp_path / f"{source}.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.executescript(fixture["setup"])
        sources.append({"id": source, "name": fixture["name"], "path": str(path)})
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
    app = live_ui.Observatory(None, tmp_path / "runs", sources=config,
                              workspace_db=tmp_path / "workspace.sqlite")

    worker_done = Event()
    original_work = app.work

    def work(*args):
        try:
            return original_work(*args)
        finally:
            worker_done.set()

    def unexpected_model(*args, **kwargs):
        raise AssertionError("This acceptance suite must not call a model")

    monkeypatch.setattr(app, "work", work)

    monkeypatch.setattr(live_ui, "LocalOllamaModel", unexpected_model)
    initial_bytes = {key: path.read_bytes() for key, path in app.databases.items()}
    with client_for(app) as client:
        for source, fixture in FIXTURE["sources"].items():
            if "metric" in fixture:
                response = client.post(f"/api/sources/{source}/knowledge", json=payload(fixture["metric"]))
                assert response.is_success, response.text

        for case in FIXTURE["cases"]:
            check_case(client, app, worker_done, case)

    assert {key: path.read_bytes() for key, path in app.databases.items()} == initial_bytes


def check_case(client, app, worker_done, case):
    source, label = case["source"], case["id"]
    conversation = client.post("/api/conversations", json={"datasource_id": source})
    assert conversation.is_success, label
    cid = conversation.json()["id"]
    worker_done.clear()
    response = client.post(f"/api/conversations/{cid}/messages", json={"question": case["question"]})
    assert response.is_success, (label, response.text)
    run_id = response.json()["id"]

    assert worker_done.wait(5), label
    detail = client.get(f"/api/conversations/{cid}")
    assert detail.is_success, label
    run = next(run for run in detail.json()["runs"] if run["id"] == run_id)
    result = run["result"]
    assert result["execution_status"] == "completed", (label, result)
    if "oracle_sql" in case:
        with closing(sqlite3.connect(app.databases[source].as_uri() + "?mode=ro", uri=True)) as db:
            oracle = [list(row) for row in db.execute(case["oracle_sql"])]
        assert oracle == case["rows"], label
        assert result["rows"] == oracle and not result["needs_clarification"], label
    else:
        assert result["needs_clarification"] and result["sql"] is None and result["rows"] == [], label
    assert all(text in result["summary"] for text in case.get("summary_contains", [])), label
