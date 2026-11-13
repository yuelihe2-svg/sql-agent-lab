"""BIRD SQLite JSONL adapter; gold queries never enter workflow input."""

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from .tools import SQLiteTools


@dataclass(frozen=True)
class Question:
    question_id: str
    db_id: str
    question: str
    evidence: str = ""


@dataclass(frozen=True)
class Reference:
    db_id: str
    sql: str


def load_bird_jsonl(path: Path, databases: dict, *, question_ids=None):
    """Read pinned/local SQLite rows with question_id, db_id, question, SQL.

    Database paths are explicitly supplied by the caller. Extra dataset fields
    (including gold schema annotations) are not forwarded to the agent.
    """
    requested = None if question_ids is None else {str(q) for q in question_ids}
    seen, dataset = set(), []
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or "question_id" not in row:
                raise ValueError(f"Line {number}: expected an object with question_id")
            qid = str(row["question_id"])
            if qid in seen:
                raise ValueError(f"Line {number}: duplicate question_id {qid}")
            seen.add(qid)
            if requested is not None and qid not in requested:
                continue
            if any(not isinstance(row.get(k), str) or not row[k].strip()
                   for k in ("db_id", "question", "SQL")):
                raise ValueError(f"Line {number}: db_id, question and SQL must be nonempty strings")
            if row["db_id"] not in databases:
                raise ValueError(f"Line {number}: database is not bound")
            evidence = row.get("evidence", "") or ""
            if not isinstance(evidence, str):
                raise ValueError(f"Line {number}: evidence must be a string")
            dataset.append((Question(qid, row["db_id"], row["question"], evidence),
                            Reference(row["db_id"], row["SQL"])))
    if not dataset or (requested is not None and requested - seen):
        raise ValueError("Dataset is empty or requested question IDs are missing")
    return dataset


def score_sql(tools: SQLiteTools, expected: Reference, actual: str) -> float:
    """BIRD-style set-of-rows EX, ignoring order and duplicates.

    Gold/environment failures propagate. Invalid predicted SQL scores zero.
    A full-result row cap is an evaluation failure, never a truncated score.
    """
    gold = tools.full_result_set(expected.db_id, expected.sql)
    try:
        predicted = tools.full_result_set(expected.db_id, actual)
    except (sqlite3.Error, ValueError):
        return 0.0
    return float(predicted == gold)
