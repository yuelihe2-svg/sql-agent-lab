"""Execution accuracy that ignores unlabeled column aliases.

shopbench_strict_v1 stays unchanged. This protocol compares the full result:
column count, value types, NULL, duplicate rows, and required row order.
Column labels are checked only when the question explicitly requires them.
Column positions still matter.
"""

from __future__ import annotations

from itertools import permutations
from pathlib import Path
import re
import time

from .sql_grader import _snapshot, fetch_rows, values_equal


SCORER = "semantic_execution_v1"
_EXPLICIT_COLUMN_NAME = re.compile(
    r"列名|字段名|列标题|别名|命名为|列必须叫|输出列必须|column name|alias",
    re.IGNORECASE,
)


def question_requires_column_names(question: str | None) -> bool:
    """True only when the question text demands specific output labels."""
    if not question:
        return False
    return _EXPLICIT_COLUMN_NAME.search(question) is not None


def _row_equal(left, right, tolerance) -> bool:
    return len(left) == len(right) and all(
        values_equal(a, b, tolerance) for a, b in zip(left, right)
    )


def _values_equal(gold_rows, predicted_rows, *, order_sensitive: bool, numeric_tolerance) -> bool:
    if len(gold_rows) != len(predicted_rows):
        return False
    if order_sensitive:
        return all(_row_equal(left, right, numeric_tolerance) for left, right in zip(gold_rows, predicted_rows))
    used = [False] * len(predicted_rows)
    for left in gold_rows:
        found = False
        for index, right in enumerate(predicted_rows):
            if used[index] or not _row_equal(left, right, numeric_tolerance):
                continue
            used[index] = True
            found = True
            break
        if not found:
            return False
    return True


def _permute(result: dict, order: tuple[int, ...]) -> dict:
    return {
        "columns": [result["columns"][index] for index in order],
        "rows": [tuple(row[index] for index in order) for row in result["rows"]],
    }


def _column_order_can_match(gold, predicted, *, order_sensitive: bool, numeric_tolerance) -> bool:
    width = len(gold["columns"])
    if width != len(predicted["columns"]) or width == 0 or width > 5:
        return False
    for order in permutations(range(width)):
        if order == tuple(range(width)):
            continue
        moved = _permute(predicted, order)
        if _values_equal(gold["rows"], moved["rows"], order_sensitive=order_sensitive,
                         numeric_tolerance=numeric_tolerance):
            return True
    return False


def grade_sql(tools, source_id, gold_sql, predicted_sql, *, order_sensitive=False,
              numeric_tolerance=None, question=None, require_column_names=None, after_gold=None) -> dict:
    """Score execution. A gold failure is an evaluation error, not a wrong answer."""
    started = time.monotonic()
    if require_column_names is None:
        require_column_names = question_requires_column_names(question)
    record = {
        "scorer": SCORER,
        "source_id": source_id,
        "status": "evaluation_error",
        "sql_correct": None,
        "reason": "unknown_source",
        "order_sensitive": order_sensitive,
        "column_names_checked": bool(require_column_names),
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
        record.update(reason="database_changed")
        record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return record
    if gold["status"] == "timeout":
        record.update(status="timeout", reason="gold_timeout")
    elif gold["status"] != "ok":
        record.update(reason="gold_sql_error", message=gold.get("message"))
    else:
        predicted = fetch_rows(tools, source_id, predicted_sql)
        if _snapshot(path) != before:
            record.update(reason="database_changed")
        elif predicted["status"] == "timeout":
            record.update(status="timeout", reason="candidate_timeout")
        elif predicted["status"] != "ok":
            record.update(status="scored", sql_correct=False, reason="candidate_sql_error",
                          message=predicted.get("message"))
        else:
            if len(gold["columns"]) != len(predicted["columns"]):
                reason = "column_count_mismatch"
            elif require_column_names and list(gold["columns"]) != list(predicted["columns"]):
                reason = "column_name_mismatch"
            elif _values_equal(gold["rows"], predicted["rows"], order_sensitive=order_sensitive,
                               numeric_tolerance=numeric_tolerance):
                reason = "match"
            elif _column_order_can_match(gold, predicted, order_sensitive=order_sensitive,
                                         numeric_tolerance=numeric_tolerance):
                reason = "column_order_mismatch"
            else:
                reason = "result_mismatch"
            record.update(status="scored", sql_correct=reason == "match", reason=reason,
                          column_count=len(gold["columns"]), row_count=len(gold["rows"]))
            record["gold_rows"] = gold
            record["predicted_rows"] = predicted
    record["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return record
