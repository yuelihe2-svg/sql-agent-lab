"""Offline ShopBench runner. Fixture mode is the default; agent mode refuses to call a model."""

from __future__ import annotations

from pathlib import Path
import argparse
import json

from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits

from .adapters.agent_adapter import VARIANTS, AgentVariantAdapter, refuse_agent_mode
from .graders.sql_grader import SCORER, fetch_rows, grade_sql
from .report import write_reports
from .shopbench_v1.catalog import materialize
from .shopbench_v1.seed import GENERATOR_VERSION, generate_all


ROOT = Path(__file__).resolve().parent


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def model_input(case: dict) -> dict:
    """Fields that may be shown to an agent. Gold SQL and context expectations stay out."""
    if "turns" in case:
        return {
            "conversation_id": case["conversation_id"],
            "turns": [
                {"turn_id": turn["turn_id"], "source_id": turn["source_id"], "question": turn["question"]}
                for turn in case["turns"]
            ],
        }
    return {
        "case_id": case["case_id"],
        "source_id": case["source_id"],
        "question": case["question"],
    }


def _public_grade(grade: dict) -> dict:
    hidden = {"gold_rows", "predicted_rows"}
    return {key: value for key, value in grade.items() if key not in hidden}


def run_fixture(output_dir: Path, *, database_dir: Path | None = None) -> dict:
    materialize(ROOT)
    database_dir = database_dir or (output_dir / "databases")
    manifest = generate_all(database_dir)
    databases = {item["source_id"]: database_dir / item["filename"] for item in manifest["databases"]}
    tools = SQLiteTools(databases)
    single_gold = load_jsonl(ROOT / "gold" / "single_turn_gold.jsonl")
    multi_gold = load_jsonl(ROOT / "gold" / "multi_turn_gold.jsonl")
    probes = load_jsonl(ROOT / "fixtures" / "predictions.jsonl")
    per_case = []
    evaluation_errors = []
    oracle_correct = 0
    oracle_eligible = 0

    def check_gold(case_id, source_id, sql, ordered, tolerance):
        nonlocal oracle_correct, oracle_eligible
        grade = grade_sql(tools, source_id, sql, sql, order_sensitive=ordered, numeric_tolerance=tolerance)
        oracle_eligible += 1
        if grade["status"] != "scored" or grade["sql_correct"] is not True:
            evaluation_errors.append({"case_id": case_id, "reason": grade["reason"], "status": grade["status"]})
        else:
            oracle_correct += 1
        per_case.append({"kind": "oracle_gold", "case_id": case_id, "source_id": source_id, **_public_grade(grade)})

    for row in single_gold:
        check_gold(row["case_id"], row["source_id"], row["sql"], row["order_sensitive"], row["numeric_tolerance"])
    for row in multi_gold:
        if row["status"] != "eligible":
            per_case.append({"kind": "oracle_gold", "case_id": row["turn_id"], "status": row["status"],
                             "reason": row.get("reason"), "sql_correct": None})
            continue
        check_gold(row["turn_id"], row["source_id"], row["sql"], row["order_sensitive"], row["numeric_tolerance"])

    gold_by_case = {row["case_id"]: row for row in single_gold}
    disagreements = []
    for probe in probes:
        gold = gold_by_case[probe["case_id"]]
        grade = grade_sql(
            tools, probe["source_id"], gold["sql"], probe["predicted_sql"],
            order_sensitive=gold["order_sensitive"], numeric_tolerance=gold["numeric_tolerance"],
        )
        if grade.get("sql_correct") is not probe["expect_sql_correct"]:
            disagreements.append(probe["probe_id"])
        per_case.append({"kind": "grader_probe", "probe_id": probe["probe_id"], "case_id": probe["case_id"],
                         "expected": probe["expect_sql_correct"], **_public_grade(grade)})

    safety_failures = _safety_checks(tools, databases)
    variants = [AgentVariantAdapter(name).run_turn() for name in VARIANTS]
    report = {
        "dataset_version": manifest["dataset_version"],
        "dataset_hashes": {item["source_id"]: {"file_sha256": item["file_sha256"], "content_sha256": item["content_sha256"]}
                           for item in manifest["databases"]},
        "generator_seed": {item["source_id"]: item["seed"] for item in manifest["databases"]},
        "generator_version": GENERATOR_VERSION,
        "agent_version": None,
        "prompt_config_hash": None,
        "model_id": None,
        "scorer_version": SCORER,
        "test_case_counts": {
            "single_turn": len(single_gold),
            "multi_turn_sessions": 20,
            "multi_turn_turns": len(multi_gold),
            "grader_probes": len(probes),
        },
        "sql_accuracy": None,
        "answer_accuracy": None,
        "turn_accuracy": None,
        "session_success": None,
        "tool_call_statistics": None,
        "latency_ms": None,
        "token_usage": {"input_tokens": None, "output_tokens": None},
        "estimated_cost": None,
        "safety_failures": safety_failures,
        "evaluation_errors": evaluation_errors,
        "unsupported_tests": ["cross_database_comparison", "conversation_memory", "retrieval_augmented_generation",
                              "multi_tenant_auth"],
        "real_agent_evaluation": "not_run",
        "variants": variants,
        "fixture_validation": {
            "label": "Fixture Validation Results",
            "gold_sql_executed": oracle_eligible,
            "gold_sql_failed": len(evaluation_errors),
            "oracle_sql_eligible": oracle_eligible,
            "oracle_sql_correct": oracle_correct,
            "probe_count": len(probes),
            "probe_disagreements": disagreements,
            "is_agent_result": False,
        },
    }
    write_reports(report, per_case, output_dir)
    if evaluation_errors or disagreements or safety_failures:
        raise SystemExit("Fixture validation failed")
    return report


def _safety_checks(tools: SQLiteTools, databases: dict[str, Path]) -> list[str]:
    failures = []
    path = databases["shop_edge"]
    before = path.read_bytes()
    for sql in ("DELETE FROM customers", "DROP TABLE customers", "SELECT 1; DELETE FROM customers"):
        result = tools.execute_sql_readonly("shop_edge", sql)
        if result["status"] != "error":
            failures.append(sql)
    if path.read_bytes() != before:
        failures.append("database bytes changed")
    try:
        tools.execute_sql_readonly("missing-db", "SELECT 1")
        failures.append("unknown db_id was accepted")
    except KeyError:
        pass
    left = fetch_rows(tools, "shop_a", "SELECT COUNT(*) AS customer_count FROM customers")
    right = fetch_rows(tools, "shop_b", "SELECT COUNT(*) AS customer_count FROM customers")
    if left["rows"] == right["rows"]:
        failures.append("shop_a and shop_b customer counts match")
    slow = SQLiteTools({"shop_edge": path}, ToolLimits(timeout_seconds=0.05))
    timed = fetch_rows(
        slow, "shop_edge",
        "WITH RECURSIVE seq(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM seq LIMIT 5000000) SELECT MAX(x) FROM seq",
    )
    if timed["status"] != "timeout":
        failures.append("timeout was not reported")
    preview = tools.execute_sql_readonly("shop_a", "SELECT customer_id FROM customers ORDER BY customer_id")
    full = fetch_rows(tools, "shop_a", "SELECT customer_id FROM customers ORDER BY customer_id")
    if not (preview["preview_row_count"] < len(full["rows"])):
        failures.append("preview was not smaller than the full result")
    sample = model_input({"case_id": "S16", "source_id": "shop_a", "question": "2026年10月净收款额是多少？"})
    encoded = json.dumps(sample, ensure_ascii=False)
    if "sql" in sample or str(path) in encoded or "WITH captured" in encoded:
        failures.append("model input contains a path or gold SQL")
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Run ShopBench v1 offline")
    parser.add_argument("--mode", choices=("fixture", "agent"), default="fixture")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-cost-usd", type=float, default=None)
    parser.add_argument("--authorize-model-call", action="store_true")
    args = parser.parse_args()
    if args.mode == "agent":
        refuse_agent_mode(model=args.model, max_cost_usd=args.max_cost_usd, authorize=args.authorize_model_call)
    run_fixture(args.output_dir)


if __name__ == "__main__":
    main()
