import json
from pathlib import Path

from sql_agent_lab.macsql.tools import SQLiteTools

from evals.adapters.agent_adapter import VARIANTS, AgentVariantAdapter, refuse_agent_mode
from evals.adapters.scripted_adapter import ScriptedConversationAdapter
from evals.graders.session_grader import summarize_sessions
from evals.graders.sql_grader import fetch_rows
from evals.runner import model_input, run_fixture


ROOT = Path(__file__).resolve().parents[2]


def _turn(**overrides):
    record = {
        "gold_status": "eligible",
        "sql_status": "scored",
        "sql_correct": True,
        "answer_status": "not_required",
        "answer_correct": None,
    }
    record.update(overrides)
    return record


def test_turn_and_session_denominators_stay_separate():
    sessions = [
        {"conversation_id": "ok", "turns": [_turn(), _turn()]},
        {"conversation_id": "mixed", "turns": [_turn(), _turn(sql_correct=False)]},
        {"conversation_id": "unsupported", "turns": [_turn(), _turn(gold_status="not_implemented", sql_status="not_implemented", sql_correct=None)]},
        {"conversation_id": "error", "turns": [_turn(sql_status="evaluation_error", sql_correct=None)]},
    ]
    summary = summarize_sessions(sessions)
    assert summary["eligible_turns"] == 5
    assert summary["correct_turns"] == 4
    assert summary["turn_accuracy"] == 4 / 5
    assert summary["eligible_sessions"] == 2
    assert summary["fully_correct_sessions"] == 1
    assert summary["session_success"] == 0.5
    assert summary["unsupported_sessions"] == ["unsupported"]
    assert summary["evaluation_error_sessions"] == ["error"]
    assert summary["not_implemented"] == 1
    assert summary["evaluation_error"] == 1


def test_scripted_history_does_not_rewrite_the_question():
    cases = [json.loads(line) for line in (ROOT / "evals" / "cases" / "multi_turn.jsonl").read_text(encoding="utf-8").splitlines()]
    gold = [json.loads(line) for line in (ROOT / "evals" / "gold" / "multi_turn_gold.jsonl").read_text(encoding="utf-8").splitlines()]
    session = next(row for row in cases if row["conversation_id"] == "M01")
    by_turn = {row["turn_id"]: row for row in gold if row["conversation_id"] == "M01"}
    adapter = ScriptedConversationAdapter({
        (session["conversation_id"], turn["turn_id"]): {"predicted_sql": by_turn[turn["turn_id"]]["sql"], "input_tokens": None}
        for turn in session["turns"]
    })
    history = []
    for turn in session["turns"]:
        result = adapter.run_turn(
            conversation_id="M01", turn_id=turn["turn_id"], question=turn["question"],
            history=list(history), source_id=turn["source_id"],
        )
        assert result["is_agent_result"] is False
        assert result["input_tokens"] is None
        assert result["question"] if False else turn["question"] == adapter.seen[-1]["question"]
        history.append(turn)
    assert "10月" not in session["turns"][1]["question"]
    assert by_turn["M01-T2"]["expected_context"]["date_range"][0] == "2026-10-01T00:00:00Z"
    payload = model_input(session)
    assert "expected_context" not in json.dumps(payload, ensure_ascii=False)
    assert "sql" not in json.dumps(payload)


def test_m20_undo_restores_the_previous_result(shopbench):
    directory, _manifest = shopbench
    tools = SQLiteTools({"shop_a": directory / "shop_a.sqlite"})
    gold = [json.loads(line) for line in (ROOT / "evals" / "gold" / "multi_turn_gold.jsonl").read_text(encoding="utf-8").splitlines()
            if '"M20-' in line]
    by_id = {row["turn_id"]: row for row in gold}
    assert by_id["M20-T2"]["expected_context"]["filters"] == ["city IN (上海, 北京)"]
    assert by_id["M20-T3"]["expected_context"] == by_id["M20-T1"]["expected_context"]
    assert by_id["M20-T4"]["sql"] == by_id["M20-T1"]["sql"]
    first = fetch_rows(tools, "shop_a", by_id["M20-T1"]["sql"])
    restored = fetch_rows(tools, "shop_a", by_id["M20-T3"]["sql"])
    assert first["rows"] == restored["rows"]
    filtered = fetch_rows(tools, "shop_a", by_id["M20-T2"]["sql"])
    assert len(filtered["rows"]) < len(first["rows"])


def test_m19_cross_database_comparison_is_not_implemented():
    gold = [json.loads(line) for line in (ROOT / "evals" / "gold" / "multi_turn_gold.jsonl").read_text(encoding="utf-8").splitlines()]
    turn = next(row for row in gold if row["turn_id"] == "M19-T4")
    assert turn["status"] == "not_implemented"
    assert turn["reason"] == "cross_database_comparison"
    assert turn["sql"] is None


def test_agent_variants_and_agent_mode_do_not_call_a_model(tmp_path):
    assert [AgentVariantAdapter(name).run_turn()["status"] for name in VARIANTS] == ["not_implemented"] * 4
    try:
        refuse_agent_mode(model=None, max_cost_usd=None, authorize=False)
    except SystemExit as error:
        assert "refused" in str(error)
    else:
        raise AssertionError("agent mode without authorization should refuse")
    try:
        refuse_agent_mode(model="local-test", max_cost_usd=1, authorize=True)
    except SystemExit as error:
        assert "will not call a model" in str(error)
    else:
        raise AssertionError("authorized agent mode still has no model client")


def test_fixture_runner_marks_real_agent_not_run(tmp_path):
    report = run_fixture(tmp_path / "report")
    assert report["real_agent_evaluation"] == "not_run"
    assert report["sql_accuracy"] is None
    assert report["token_usage"] == {"input_tokens": None, "output_tokens": None}
    assert report["estimated_cost"] is None
    assert report["fixture_validation"]["is_agent_result"] is False
    assert report["fixture_validation"]["gold_sql_failed"] == 0
    assert report["fixture_validation"]["probe_disagreements"] == []
    assert (tmp_path / "report" / "report.json").exists()
    assert (tmp_path / "report" / "report.md").exists()
    assert (tmp_path / "report" / "per_case.jsonl").exists()
