"""Write fixture reports. Real agent numbers stay null until a model run exists."""

from __future__ import annotations

from pathlib import Path
import json


def _line(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: dict) -> str:
    fixture = report["fixture_validation"]
    return "\n".join([
        "# ShopBench v1 report",
        "",
        "Real Agent Evaluation Results: not_run",
        "",
        "No model was called. Fixture Validation Results below check the dataset, gold SQL, and grader. They are not an agent accuracy.",
        "",
        f"- Dataset version: {report['dataset_version']}",
        f"- Generator seeds: {json.dumps(report['generator_seed'], ensure_ascii=False)}",
        f"- Dataset hashes: {json.dumps(report['dataset_hashes'], ensure_ascii=False)}",
        f"- Agent version: {_line(report['agent_version'])}",
        f"- Prompt/config hash: {_line(report['prompt_config_hash'])}",
        f"- Model ID: {_line(report['model_id'])}",
        f"- Scorer version: {report['scorer_version']}",
        f"- Test case counts: {json.dumps(report['test_case_counts'], ensure_ascii=False)}",
        f"- SQL Accuracy: {_line(report['sql_accuracy'])}",
        f"- Answer Accuracy: {_line(report['answer_accuracy'])}",
        f"- Turn Accuracy: {_line(report['turn_accuracy'])}",
        f"- Session Success: {_line(report['session_success'])}",
        f"- Tool call statistics: {_line(report['tool_call_statistics'])}",
        f"- Latency ms: {_line(report['latency_ms'])}",
        f"- Token usage: {json.dumps(report['token_usage'], ensure_ascii=False)}",
        f"- Estimated cost: {_line(report['estimated_cost'])}",
        f"- Safety failures: {json.dumps(report['safety_failures'], ensure_ascii=False)}",
        f"- Evaluation errors: {json.dumps(report['evaluation_errors'], ensure_ascii=False)}",
        f"- Unsupported tests: {json.dumps(report['unsupported_tests'], ensure_ascii=False)}",
        "",
        "## Fixture Validation Results",
        "",
        f"- Gold SQL executed: {fixture['gold_sql_executed']}",
        f"- Gold SQL failed: {fixture['gold_sql_failed']}",
        f"- Grader probes: {fixture['probe_count']}",
        f"- Probe disagreements: {fixture['probe_disagreements']}",
        f"- Oracle SQL self-check correct / eligible: {fixture['oracle_sql_correct']} / {fixture['oracle_sql_eligible']}",
        "",
        "Denominators for real agent metrics exclude not_implemented, evaluation_error, timeout, and skipped turns. Those counts are listed separately and are not folded into accuracy.",
        "",
    ])


def write_reports(report: dict, cases: list[dict], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    (output_dir / "per_case.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cases),
        encoding="utf-8",
    )
