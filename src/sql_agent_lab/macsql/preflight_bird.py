"""Validate local BIRD assets and reference SQL without importing a model SDK.

This is a data/tool compatibility check, not model evaluation. Question selection
uses only IDs, database and difficulty metadata, before observing SQL outcomes.
"""

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .context import ContextPolicy, byte_token_bound
from .dataset import load_bird_jsonl, score_sql
from .fetch_bird import sha256
from .reference_baseline import ReferenceMacSQLAgent, _messages, schema_text
from .tools import SQLiteTools, ToolLimits


def select_questions(rows, seed=123):
    """Two per database (easiest available + challenging), then two moderate.

    This 24-question integration subset deliberately covers schemas/difficulty;
    it is not a representative sample for a benchmark accuracy claim.
    """
    rank = lambda row: hashlib.sha256(f"{seed}:{row['question_id']}".encode()).hexdigest()
    chosen = []
    for db in sorted({r["db_id"] for r in rows}):
        group = sorted([r for r in rows if r["db_id"] == db], key=rank)
        first = min(group, key=lambda r: ({"simple": 0, "moderate": 1, "challenging": 2}[r["difficulty"]], rank(r)))
        chosen.append(first)
        second = next(r for r in group if r["difficulty"] == "challenging" and r != first)
        chosen.append(second)
    for row in sorted(rows, key=rank):
        if len(chosen) >= 24:
            break
        if row["difficulty"] == "moderate" and row not in chosen:
            chosen.append(row)
    if len(chosen) != 24:
        raise ValueError("Cannot construct the documented 24-question subset")
    return sorted(chosen, key=lambda r: r["question_id"])


def preflight(data_dir, output_dir, timeout=30.0):
    data_dir, output_dir = Path(data_dir).resolve(), Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_lock = json.loads((data_dir / "source-lock.json").read_text())
    dataset_path = data_dir / "mini_dev_sqlite.jsonl"
    if sha256(dataset_path) != source_lock["annotations_sha256"]:
        raise ValueError("Annotation hash mismatch")
    databases = {db: data_dir / item["path"] for db, item in source_lock["databases"].items()}
    for db, path in databases.items():
        if sha256(path) != source_lock["databases"][db]["sha256"]:
            raise ValueError(f"Database hash mismatch: {db}")
    rows = [json.loads(line) for line in dataset_path.read_text(encoding="utf-8").splitlines()]
    selected = select_questions(rows)
    selected_ids = [r["question_id"] for r in selected]
    pilot_ids = [r["question_id"] for r in selected if r["difficulty"] == "simple"][:2]
    subset_manifest = {
        "purpose": "integration acceptance; not representative benchmark evaluation",
        "selection": "seed 123: 2/database (easiest available + challenging), then 2 moderate; no SQL outcomes used",
        "hf_revision": source_lock["hf_revision"], "annotations_sha256": source_lock["annotations_sha256"],
        "question_ids": selected_ids, "pilot_ids": pilot_ids,
        "difficulty_counts": dict(Counter(r["difficulty"] for r in selected)),
        "database_counts": dict(Counter(r["db_id"] for r in selected)),
    }
    (output_dir / "subset.json").write_text(json.dumps(subset_manifest, indent=2), encoding="utf-8")
    (output_dir / "subset24.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in selected), encoding="utf-8")
    (output_dir / "databases.json").write_text(json.dumps({db: str(p) for db, p in databases.items()}, indent=2), encoding="utf-8")
    limits = ToolLimits(timeout_seconds=timeout)
    tools = SQLiteTools(databases, limits)
    dataset = load_bird_jsonl(dataset_path, databases)
    selected_dataset = load_bird_jsonl(dataset_path, databases, question_ids=selected_ids)
    policy = ContextPolicy()
    schemas, db_checks = {}, {}
    for db in databases:
        with tools.connection(db) as conn:
            check = conn.execute("PRAGMA quick_check").fetchall()
        schema = tools.load_schema(db, enrich=True)
        schemas[db] = schema
        db_checks[db] = {"quick_check": check, "tables": len(schema),
                         "columns": sum(len(t["columns"]) for t in schema.values())}
    checks = []
    for question, reference in dataset:
        check = {"question_id": question.question_id, "db_id": question.db_id}
        start = time.monotonic()
        try:
            result = tools.full_result_set(reference.db_id, reference.sql)
            check.update(status="ok", distinct_result_rows=len(result),
                         result_digest=hashlib.sha256(repr(sorted(result, key=repr)).encode()).hexdigest())
        except Exception as error:
            check.update(status="error", error_type=type(error).__name__, error=str(error))
        check["elapsed_seconds"] = time.monotonic() - start
        checks.append(check)
        if len(checks) % 50 == 0:
            print(f"Checked {len(checks)}/500 reference queries", flush=True)
    prompt_checks = []
    for question, reference in selected_dataset:
        schema = schemas[question.db_id]
        # Construct complete-schema prompts as an upper-envelope context probe;
        # these are never sent to a model, and do not include reference.sql.
        record = {"question_id": question.question_id, "db_id": question.db_id, "roles": {}}
        rendered = schema_text(schema)
        prompts = {
            "selector": ReferenceMacSQLAgent._selector_prompt(None, question.question, question.evidence, rendered),
            "decomposer": ReferenceMacSQLAgent._decomposer_prompt(None, question.question, question.evidence, rendered),
            "refiner": ReferenceMacSQLAgent._refiner_prompt(
                None, question.question, question.evidence, rendered,
                "SELECT missing FROM missing_table",
                {"status": "error", "message": "no such table: missing_table"}),
        }
        for role, prompt in prompts.items():
            messages = _messages(prompt)
            units = byte_token_bound(messages)
            record["roles"][role] = {"input_byte_bound": units, "fits_default_input_budget": units <= policy.input_budget}
        record["selector_route"] = ("skip" if record["roles"]["decomposer"]["fits_default_input_budget"]
                                    else "select_columns")
        try:
            record["reference_self_score"] = score_sql(tools, reference, reference.sql)
            record["invalid_sql_score"] = score_sql(tools, reference, "SELECT no_such_column FROM no_such_table")
        except Exception as error:
            record["scoring_error"] = str(error)
        assert set(asdict(question)) == {"question_id", "db_id", "question", "evidence"}
        prompt_checks.append(record)
    # Rehash after execution to establish that all local SQL checks were read-only.
    unchanged = all(sha256(path) == source_lock["databases"][db]["sha256"] for db, path in databases.items())
    errors = [c for c in checks if c["status"] != "ok"]
    ready = (not errors and unchanged and
             all(c["quick_check"] == [("ok",)] for c in db_checks.values()) and
             all(c.get("reference_self_score") == 1.0 and c.get("invalid_sql_score") == 0.0
                 for c in prompt_checks))
    report = {
        "kind": "offline_data_preflight", "real_api_calls": 0, "sqlite_version": sqlite3.sqlite_version,
        "hf_revision": source_lock["hf_revision"], "tool_limits": asdict(limits),
        "questions": len(dataset), "database_count": len(databases), "database_checks": db_checks,
        "gold_ok": len(checks) - len(errors), "gold_errors": errors,
        "gold_empty_results": [c["question_id"] for c in checks if c.get("distinct_result_rows") == 0],
        "databases_unchanged": unchanged, "ready": ready,
        "subset": subset_manifest, "context_checks": prompt_checks,
        "gold_query_checks": checks,
    }
    (output_dir / "preflight.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("questions", "database_count", "gold_ok", "gold_errors", "databases_unchanged")}))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    source_root = Path(__file__).resolve().parents[3]
    if args.output_dir.resolve() == source_root or source_root in args.output_dir.resolve().parents:
        parser.error("Output must stay outside the source checkout")
    report = preflight(args.data_dir, args.output_dir, args.timeout)
    if not report["ready"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
