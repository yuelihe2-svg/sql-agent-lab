"""Strict full-result grader. Preview rows are never a score."""

from __future__ import annotations

from pathlib import Path
import hashlib
import sqlite3
import time

SCORER = "shopbench_strict_v1"


def values_equal(left, right, tolerance):
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if tolerance is None:
            return type(left) is type(right) and left == right
        return abs(float(left) - float(right)) <= tolerance
    return type(left) is type(right) and left == right


def rows_equal(gold, predicted, *, order_sensitive: bool, numeric_tolerance) -> bool:
    if gold["columns"] != predicted["columns"]:
        return False
    gold_rows = gold["rows"]
    predicted_rows = predicted["rows"]
    if len(gold_rows) != len(predicted_rows):
        return False
    if order_sensitive:
        return all(
            len(left) == len(right) and all(values_equal(a, b, numeric_tolerance) for a, b in zip(left, right))
            for left, right in zip(gold_rows, predicted_rows)
        )
    used = [False] * len(predicted_rows)
    for left in gold_rows:
        found = False
        for index, right in enumerate(predicted_rows):
            if used[index] or len(left) != len(right):
                continue
            if all(values_equal(a, b, numeric_tolerance) for a, b in zip(left, right)):
                used[index] = True
                found = True
                break
        if not found:
            return False
    return True


def fetch_rows(tools, source_id, sql) -> dict:
    if source_id not in tools.databases:
        return {"status": "error", "reason": "unknown_source", "message": "database is not bound"}
    try:
        with tools.connection(source_id, query=True) as conn:
            cursor = tools._execute(conn, sql)
            columns = [item[0] for item in cursor.description]
            rows = [tuple(row) for row in cursor.fetchall()]
        return {"status": "ok", "columns": columns, "rows": rows}
    except sqlite3.OperationalError as error:
        text = str(error)
        if "interrupt" in text.lower():
            return {"status": "timeout", "reason": "timeout", "message": text[:500]}
        return {"status": "error", "reason": "sql_error", "message": text[:500]}
    except (sqlite3.Error, ValueError) as error:
        return {"status": "error", "reason": "sql_error", "message": str(error)[:500]}


def _snapshot(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def grade_case(tools, source_id, gold_sql, predicted_sql, **kwargs) -> dict:
    if not gold_sql:
        return {"scorer": SCORER, "source_id": source_id, "status": "skipped",
                "sql_correct": None, "reason": "no_reference"}
    return grade_sql(tools, source_id, gold_sql, predicted_sql, **kwargs)


def grade_sql(tools, source_id, gold_sql, predicted_sql, *, order_sensitive=False,
              numeric_tolerance=None, after_gold=None) -> dict:
    """Compare full results. Gold failure is an evaluation error, not a wrong answer."""
    started = time.monotonic()
    record = {
        "scorer": SCORER,
        "source_id": source_id,
        "status": "evaluation_error",
        "sql_correct": None,
        "reason": "unknown_source",
        "order_sensitive": order_sensitive,
    }
    if source_id not in getattr(tools, "databases", {}):
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return record
    path = Path(tools.databases[source_id])
    before = _snapshot(path)
    gold = fetch_rows(tools, source_id, gold_sql)
    if after_gold is not None:
        after_gold()
    if _snapshot(path) != before:
        record.update(reason="database_changed", status="evaluation_error")
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return record
    if gold["status"] == "timeout":
        record.update(status="timeout", reason="gold_timeout")
    elif gold["status"] != "ok":
        record.update(status="evaluation_error", reason="gold_sql_error", message=gold.get("message"))
    else:
        predicted = fetch_rows(tools, source_id, predicted_sql)
        if _snapshot(path) != before:
            record.update(reason="database_changed", status="evaluation_error")
        elif predicted["status"] == "timeout":
            record.update(status="timeout", reason="candidate_timeout")
        elif predicted["status"] != "ok":
            record.update(status="scored", sql_correct=False, reason="candidate_sql_error",
                          message=predicted.get("message"))
        else:
            match = rows_equal(gold, predicted, order_sensitive=order_sensitive,
                               numeric_tolerance=numeric_tolerance)
            record.update(status="scored", sql_correct=match,
                          reason="match" if match else "result_mismatch",
                          column_count=len(gold["columns"]), row_count=len(gold["rows"]))
            record["gold_rows"] = gold
            record["predicted_rows"] = predicted
    record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return record
