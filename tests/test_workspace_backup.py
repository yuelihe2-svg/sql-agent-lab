from contextlib import closing
import base64
import json
import sqlite3
from threading import Event

import pytest

from sql_agent_lab.macsql import live_ui
from sql_agent_lab.macsql.fetch_bird import sha256
from sql_agent_lab.workspace.backup import create_backup, restore_backup, verify_backup
from test_conversations_api import build, wait_done


def test_portable_restore_preserves_results_metric_and_upload_without_replaying_tasks(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    upload = app.upload_database({
        "name": "恢复验收库",
        "data": base64.b64encode(app.databases["shop"].read_bytes()).decode(),
    })["source"]["id"]
    with closing(sqlite3.connect(app.databases[upload])) as conn:
        conn.executescript("CREATE TABLE sales(y INTEGER, code INTEGER, amount INTEGER);"
                           "INSERT INTO sales VALUES (2024,1,60),(2024,0,999);")
    from sql_agent_lab.workspace.metric_definition import PAID_TOTAL_TITLE
    config = dict(table="sales", year_column="y", status_column="code", amount_column="amount", paid_value=1)
    app.add_source_knowledge(upload, {"title": PAID_TOTAL_TITLE, "format": "json", "body": json.dumps(config)})
    cid = app.create_conversation({"datasource_id": upload})["id"]
    question = "统计2024年已付款订单总金额"
    first = app.post_conversation_message(cid, {"question": question})["id"]
    wait_done(app)
    monkeypatch.setattr(app, "_dispatch_pending", lambda **kwargs: None)
    queued = app.post_conversation_message(cid, {"question": question})["id"]
    saved = app.output / queued / "task.json"
    original_task = saved.read_bytes()
    before = {source: sha256(path) for source, path in app.databases.items()}

    bundle, moved, recovered = (tmp_path / name for name in ("bundle", "moved", "restored"))
    count = create_backup(app.workspace_db, app.output, bundle, sources=tmp_path / "sources.json")
    bundle.rename(moved)
    assert len(verify_backup(moved)) == count
    assert restore_backup(moved, recovered) == count
    assert saved.read_bytes() == original_task
    assert {source: sha256(path) for source, path in app.databases.items()} == before

    def no_model(*args, **kwargs):
        raise AssertionError("Offline restore verification must not initialize a model")

    monkeypatch.setattr(live_ui, "LocalOllamaModel", no_model)
    restored = live_ui.Observatory(None, recovered / "runs", sources=recovered / "sources.json",
                                  workspace_db=recovered / "workspace.sqlite")
    assert not restored.pending
    detail = restored.conversation_detail(cid)
    assert [(r["id"], r["state"]) for r in detail["runs"]] == [(first, "completed"), (queued, "interrupted")]
    assert detail["runs"][0]["result"]["rows"] == [[60]]
    assert len(detail["messages"]) == 2
    assert restored._workspace().get_agent_run(first).artifact_dir == str(recovered / "runs" / first)
    assert json.loads(restored.list_source_knowledge(upload)["knowledge"][0]["body"]) == config
    assert restored.databases[upload].parent == recovered / "databases"

    restored.worker_done = Event()
    work = restored.work

    def completed(*args):
        try:
            work(*args)
        finally:
            restored.worker_done.set()

    monkeypatch.setattr(restored, "work", completed)
    ask_again = restored.create_conversation({"datasource_id": upload})["id"]
    restored.post_conversation_message(ask_again, {"question": question})
    wait_done(restored)
    assert restored.conversation_detail(ask_again)["runs"][0]["result"]["rows"] == [[60]]


def test_backup_captures_wal_and_rejects_tamper_or_overwrite(tmp_path, monkeypatch):
    app = build(tmp_path, monkeypatch)
    bundle = tmp_path / "bundle"
    with closing(sqlite3.connect(app.databases["shop"])) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO numbers VALUES (123)")
        writer.commit()
        assert app.databases["shop"].with_suffix(".sqlite-wal").is_file()
        create_backup(app.workspace_db, app.output, bundle, sources=tmp_path / "sources.json")
    with closing(sqlite3.connect(bundle / "databases" / "shop.sqlite")) as db:
        assert db.execute("SELECT n FROM numbers ORDER BY n").fetchall() == [(42,), (123,)]

    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "keep.txt"
    marker.write_text("unchanged", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        restore_backup(bundle, existing)
    assert marker.read_text(encoding="utf-8") == "unchanged"
    (bundle / "sources.json").write_text("{}", encoding="utf-8")
    rejected = tmp_path / "rejected"
    with pytest.raises(ValueError, match="checksum mismatch"):
        restore_backup(bundle, rejected)
    assert not rejected.exists()
