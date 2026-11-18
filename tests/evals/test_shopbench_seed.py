from pathlib import Path
import sqlite3

from evals.shopbench_v1.seed import SPECS, generate_all, validate_connection


EXPECTED = {
    "shop_a": {"customers": 120, "categories": 8, "products": 50, "orders": 600, "order_items": 1400},
    "shop_b": {"customers": 96, "categories": 8, "products": 42, "orders": 480, "order_items": 1100},
    "shop_edge": {"customers": 20, "categories": 8, "products": 15, "orders": 35, "order_items": 70},
}


def test_fixed_seeds_and_row_counts(shopbench):
    directory, manifest = shopbench
    by_id = {item["source_id"]: item for item in manifest["databases"]}
    assert [spec.seed for spec in SPECS] == [20261118, 20261119, 20261120]
    for spec in SPECS:
        record = by_id[spec.source_id]
        assert record["seed"] == spec.seed
        assert record["row_counts"] | EXPECTED[spec.source_id] == record["row_counts"]
        for table, count in EXPECTED[spec.source_id].items():
            assert record["row_counts"][table] == count
        conn = sqlite3.connect(directory / record["filename"])
        try:
            assert validate_connection(conn) == []
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            conn.close()


def test_generation_is_repeatable(tmp_path):
    first = generate_all(tmp_path / "one")
    second = generate_all(tmp_path / "two")
    for left, right in zip(first["databases"], second["databases"]):
        assert left["file_sha256"] == right["file_sha256"]
        assert left["content_sha256"] == right["content_sha256"]
        assert left["schema_sha256"] == right["schema_sha256"]


def test_sources_differ_and_edge_boundaries_exist(shopbench):
    directory, manifest = shopbench
    nets = {}
    for item in manifest["databases"]:
        conn = sqlite3.connect(directory / item["filename"])
        nets[item["source_id"]] = conn.execute(
            """
            SELECT COALESCE((SELECT SUM(amount_cents) FROM payments
                             WHERE status = 'succeeded'
                               AND paid_at >= '2026-10-01T00:00:00Z'
                               AND paid_at < '2026-11-01T00:00:00Z'), 0)
                 - COALESCE((SELECT SUM(amount_cents) FROM refunds
                             WHERE status = 'completed'
                               AND completed_at >= '2026-10-01T00:00:00Z'
                               AND completed_at < '2026-11-01T00:00:00Z'), 0)
            """
        ).fetchone()[0]
        conn.close()
    assert nets["shop_a"] != nets["shop_b"]
    edge = sqlite3.connect(directory / "shop_edge.sqlite")
    try:
        assert edge.execute("SELECT COUNT(*) FROM customers WHERE email IS NULL").fetchone()[0] == 3
        assert edge.execute(
            "SELECT COUNT(*) FROM payments WHERE paid_at = '2026-10-01T00:00:00Z' AND status = 'succeeded'"
        ).fetchone()[0] >= 1
        assert edge.execute(
            "SELECT COUNT(*) FROM payments WHERE paid_at = '2026-11-01T00:00:00Z' AND status = 'succeeded'"
        ).fetchone()[0] >= 1
        assert edge.execute(
            "SELECT COUNT(*) FROM payments WHERE paid_at >= '2026-12-01T00:00:00Z'"
        ).fetchone()[0] == 0
        assert edge.execute("SELECT signup_at FROM customers WHERE customer_id = 1").fetchone()[0] == "2026-10-01T00:00:00Z"
        assert edge.execute("SELECT signup_at FROM customers WHERE customer_id = 2").fetchone()[0] == "2026-11-01T00:00:00Z"
    finally:
        edge.close()


def test_sqlite_files_are_not_source_files():
    root = Path(__file__).resolve().parents[2]
    assert list(root.joinpath("evals").rglob("*.sqlite")) == []
