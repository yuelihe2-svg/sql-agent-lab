import json
from pathlib import Path

from sql_agent_lab.macsql.tools import SQLiteTools

from evals.adapters.offline_agent import grade_prediction, load_scripts, run_scripted_case
from evals.runner import run_fixture


ROOT = Path(__file__).resolve().parents[2]
COVERAGE = {
    "single_table",
    "join",
    "group_by",
    "net_receipts",
    "null",
    "empty_result",
    "repeated_payment_refund",
    "sql_repair",
    "datasource_switch",
    "execution_rejected",
}


def _gold_by_case():
    rows = [
        json.loads(line)
        for line in (ROOT / "evals" / "gold" / "single_turn_gold.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    return {row["case_id"]: row for row in rows}


def test_offline_three_role_path_scores_full_results_without_leaking_gold(shopbench):
    directory, _manifest = shopbench
    before = {name: (directory / f"{name}.sqlite").read_bytes() for name in ("shop_a", "shop_b")}
    tools = SQLiteTools({name: directory / f"{name}.sqlite" for name in ("shop_a", "shop_b")})
    scripts = load_scripts()
    assert COVERAGE <= {script["coverage"] for script in scripts}
    gold = _gold_by_case()
    executions = []
    graded = []
    for script in scripts:
        assert "gold" not in json.dumps({key: script[key] for key in script if key != "responses"})
        execution = run_scripted_case(script, tools)
        executions.append(execution)
        visible = json.dumps(execution["model_inputs"], ensure_ascii=False)
        assert gold[script["case_id"]]["sql"] not in visible
        assert execution["run_error"] is None
        assert execution["roles"] == script["expect_roles"]
        assert "selector" not in execution["roles"]
        assert "semantic_review" not in execution["trace_events"]
        graded.append(grade_prediction(execution, tools, gold[script["case_id"]]))
        assert graded[-1]["sql_correct"] is script["expect_sql_correct"]
        assert graded[-1]["sql_execution_status"] == script["expect_execution_status"]
        assert graded[-1]["real_agent_evaluation"] == "not_run"
        if script.get("input_tokens") is None:
            assert graded[-1]["input_tokens"] is None
            assert graded[-1]["output_tokens"] is None
    by_id = {row["script_id"]: row for row in graded}
    assert by_id["OFF-S16"]["sql_correct"] is True
    assert by_id["OFF-S16-WRONG"]["sql_correct"] is False
    assert by_id["OFF-S16-WRONG"]["error_category"] == "result_mismatch"
    assert by_id["OFF-REJECT"]["sql_correct"] is False
    assert by_id["OFF-REJECT"]["error_category"] == "execution_rejected"
    assert by_id["OFF-REPAIR"]["repair_count"] == 1
    assert by_id["OFF-S17"]["tool_calls"] >= 2
    shop_a = next(row for row in executions if row["script_id"] == "OFF-S01")
    shop_b = next(row for row in executions if row["script_id"] == "OFF-S01-B")
    assert shop_a["predicted_sql"] == shop_b["predicted_sql"]
    assert by_id["OFF-S01"]["datasource_id"] == "shop_a"
    assert by_id["OFF-S01-B"]["datasource_id"] == "shop_b"
    counts = {
        row["datasource_id"]: tools.execute_sql_readonly(row["datasource_id"], row["predicted_sql"])["preview_rows"]
        for row in (shop_a, shop_b)
    }
    assert counts["shop_a"] == [[120]]
    assert counts["shop_b"] == [[96]]
    assert {name: (directory / f"{name}.sqlite").read_bytes() for name in before} == before


def test_fixture_runner_still_marks_real_agent_not_run(tmp_path):
    report = run_fixture(tmp_path / "fixture")
    assert report["real_agent_evaluation"] == "not_run"
    assert report["sql_accuracy"] is None
