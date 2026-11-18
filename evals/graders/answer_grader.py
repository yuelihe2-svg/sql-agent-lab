"""Structured answer checks. Free-form prose stays manual_review; no model judge."""

from __future__ import annotations

import re

from .sql_grader import rows_equal

_INTEGER = re.compile(r"(?<![\w.])-?\d+(?![\w.])")


def _integers(text: str) -> list[int]:
    return [int(item) for item in _INTEGER.findall(text or "")]


def _cells(result) -> set:
    values = set()
    if not result or result.get("status") != "ok":
        return values
    for row in result["rows"]:
        for value in row:
            values.add(value)
    return values


def grade_answer(*, gold, execution, answer, question="", order_sensitive=False, numeric_tolerance=None) -> dict:
    if not answer or answer.get("mode") == "manual_review":
        return {"status": "manual_review", "answer_correct": None, "faithfulness": "manual_review"}
    text = answer.get("text") or ""
    if "rows" not in answer and not text:
        return {"status": "manual_review", "answer_correct": None, "faithfulness": "manual_review"}
    stated = None
    if "rows" in answer:
        stated = {
            "columns": list(answer.get("columns") or gold.get("columns") or []),
            "rows": [tuple(row) for row in answer["rows"]],
        }
        answer_correct = rows_equal(
            {"columns": gold["columns"], "rows": gold["rows"]},
            stated,
            order_sensitive=order_sensitive,
            numeric_tolerance=numeric_tolerance,
        )
    else:
        answer_correct = None
    faithfulness = "manual_review"
    if execution and execution.get("status") == "ok":
        present = _cells(execution)
        question_numbers = set(_integers(question))
        extras = [
            number for number in _integers(text)
            if number not in question_numbers and number not in present and str(number) not in {str(value) for value in present}
        ]
        structured_ok = True
        if stated is not None:
            structured_ok = rows_equal(execution, stated, order_sensitive=order_sensitive,
                                       numeric_tolerance=numeric_tolerance)
        faithfulness = structured_ok and not extras
        if answer_correct is None and stated is None:
            gold_values = _cells({"status": "ok", "rows": gold["rows"]})
            if len(gold["rows"]) == 1 and len(gold["rows"][0]) == 1:
                expected = gold["rows"][0][0]
                answer_correct = expected in _integers(text) or str(expected) in text
            else:
                answer_correct = None
                faithfulness = "manual_review" if not extras else False
    status = "scored" if answer_correct is not None else "manual_review"
    return {"status": status, "answer_correct": answer_correct, "faithfulness": faithfulness}
