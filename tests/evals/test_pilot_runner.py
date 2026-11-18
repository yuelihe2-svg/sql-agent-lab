"""Offline checks for the local pilot runner. No Ollama server is started."""

import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest

from evals.graders.sql_grader import fetch_rows
from evals.pilot.runner import (
    DEFAULT_RUNTIME_NUM_CTX,
    MAX_CASES,
    PILOT_CASE_IDS,
    PilotRefused,
    _evaluation_outcome,
    _is_scored,
    context_length_from_show,
    main,
    prepare_live_model,
    prompt_config_hash,
    require_context_agreement,
    run_pilot,
)
from sql_agent_lab.macsql.reference_core import ModelReply
from sql_agent_lab.macsql.tools import SQLiteTools


WINDOW = 32_000
BUDGET = dict(actual_context_length=WINDOW, context_window=WINDOW, input_budget=24_000, output_reserve=1_000)
S01_SQL = "SELECT COUNT(*) AS customer_count FROM customers"


class FakeModel:
    def __init__(self, *, fail_on=None, sleep_first=0):
        self.fail_on = fail_on
        self.sleep_first = sleep_first
        self.calls = 0
        self.messages = []

    def __call__(self, *, role, model, messages, max_output_tokens):
        self.calls += 1
        self.messages.extend(item["content"] for item in messages)
        if self.fail_on == self.calls:
            raise RuntimeError("fake model failed")
        if self.sleep_first and self.calls == 1:
            time.sleep(self.sleep_first)
        content = messages[-1]["content"]
        if "数据库共有多少客户" in content:
            sql = S01_SQL
        elif "各个城市有多少客户" in content:
            sql = "SELECT city, COUNT(*) AS customer_count FROM customers GROUP BY city"
        else:
            sql = "SELECT 1 AS wrong_answer"
        return ModelReply("```sql\n" + sql + "\n```", 4, 3, simulated=True, response_id="fake")


def _hashes(manifest):
    return {item["source_id"]: item["file_sha256"] for item in manifest["databases"]}


def _digest(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, default=str).encode()).hexdigest()


def _run(tmp_path, database_dir, manifest, model, **overrides):
    args = dict(
        model=model,
        expected_hashes=_hashes(manifest),
        model_id="fake-script",
        model_version=None,
        live_model=False,
        **BUDGET,
    )
    args.update(overrides)
    return run_pilot(tmp_path / "pilot", database_dir, **args)


def test_manifest_lists_fixed_cases_and_hashes(shopbench, tmp_path):
    database_dir, manifest = shopbench
    model = FakeModel()
    report = _run(tmp_path, database_dir, manifest, model, max_cases=1)
    saved = json.loads((tmp_path / "pilot" / "manifest.json").read_text(encoding="utf-8"))
    assert saved["case_ids"] == list(PILOT_CASE_IDS)
    assert len(saved["case_ids"]) == MAX_CASES == 12
    assert set(saved["coverage"].values()) == {
        "simple_query", "simple_filter", "aggregation_join", "aggregation", "payment_aggregate",
        "net_receipts", "join", "unmatched_customers", "repeated_payments", "repeated_refunds",
        "null", "empty_result",
    }
    assert saved["database_sha256"]["shop_a"] == _hashes(manifest)["shop_a"]
    assert saved["model_id"] == "fake-script"
    assert saved["model_version"] is None
    assert saved["prompt_config_hash"] == prompt_config_hash(context_window=WINDOW, input_budget=24_000, output_reserve=1_000)
    assert saved["temperature"] == 0
    assert saved["scorer"] == "shopbench_strict_v1"
    assert saved["semantic_review"] is False and saved["max_repairs"] == 1
    assert saved["estimated_cost"] is None
    assert saved["real_agent_evaluation"] == "not_run"
    assert report["bound_sources"] == ["shop_a"]
    assert (tmp_path / "pilot" / "summary.md").exists()


def test_hash_mismatch_and_unbound_source_do_not_call_the_model(shopbench, tmp_path):
    database_dir, manifest = shopbench
    model = FakeModel()
    hashes = _hashes(manifest)
    hashes["shop_a"] = "0" * 64
    with pytest.raises(PilotRefused, match="hash mismatch"):
        _run(tmp_path, database_dir, manifest, model, expected_hashes=hashes)
    assert model.calls == 0
    hashes = {key: value for key, value in _hashes(manifest).items() if key != "shop_a"}
    with pytest.raises(PilotRefused, match="no hash"):
        _run(tmp_path / "other", database_dir, manifest, model, expected_hashes=hashes)
    assert model.calls == 0


def test_budget_stop_resume_and_database_identity(shopbench, tmp_path):
    database_dir, manifest = shopbench
    model = FakeModel()
    first = _run(tmp_path, database_dir, manifest, model, max_cases=1)
    assert first["stopped_reason"] == "max_cases"
    assert [row["case_id"] for row in first["results"]] == ["S01"]
    assert first["results"][0]["sql_correct"] is True
    assert first["results"][0]["estimated_cost"] is None
    assert model.calls == 1
    tools = SQLiteTools({
        "shop_a": database_dir / "shop_a.sqlite",
        "shop_b": database_dir / "shop_b.sqlite",
    })
    shop_a = fetch_rows(tools, "shop_a", S01_SQL)
    shop_b = fetch_rows(tools, "shop_b", S01_SQL)
    assert shop_a["rows"] != shop_b["rows"]
    assert first["results"][0]["predicted_result_sha256"] == _digest(shop_a["rows"])
    assert first["results"][0]["datasource_id"] == "shop_a"

    before = (tmp_path / "pilot" / "results.jsonl").read_text(encoding="utf-8")
    second = _run(tmp_path, database_dir, manifest, model, max_cases=2)
    assert model.calls == 2
    assert [row["case_id"] for row in second["results"]] == ["S01", "S03"]
    assert second["results"][0]["sql_correct"] is True
    assert second["results"][1]["sql_correct"] is False
    assert second["results"][1]["error_category"] == "result_mismatch"
    journal = (tmp_path / "pilot" / "results.jsonl").read_text(encoding="utf-8")
    assert journal.startswith(before)
    assert journal.count('"case_id": "S01"') == 1

    saved = json.loads((tmp_path / "pilot" / "manifest.json").read_text(encoding="utf-8"))
    saved["model_id"] = "other-model"
    (tmp_path / "pilot" / "manifest.json").write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(PilotRefused, match="does not match"):
        _run(tmp_path, database_dir, manifest, model, max_cases=3)
    assert model.calls == 2


def test_gold_sql_stays_out_of_model_input(shopbench, tmp_path):
    database_dir, manifest = shopbench
    model = FakeModel()
    _run(tmp_path, database_dir, manifest, model, max_cases=MAX_CASES)
    gold = {
        json.loads(line)["case_id"]: json.loads(line)["sql"]
        for line in Path("evals/gold/single_turn_gold.jsonl").read_text(encoding="utf-8").splitlines()
    }
    visible = "\n".join(model.messages)
    for case_id in PILOT_CASE_IDS:
        assert gold[case_id] not in visible
    assert "schema_retrieved" not in visible
    assert all(row["input_tokens"] == 4 and row["output_tokens"] == 3 for row in
               json.loads("[" + (tmp_path / "pilot" / "results.jsonl").read_text(encoding="utf-8").replace("}\n{", "},{") + "]")
               if row["sql_execution_status"] == "ok")


def test_model_exception_records_null_usage_and_continues(shopbench, tmp_path):
    database_dir, manifest = shopbench
    model = FakeModel(fail_on=1)
    report = _run(tmp_path, database_dir, manifest, model, max_cases=2)
    assert report["results"][0]["evaluation_status"] == "evaluation_error"
    assert report["results"][0]["sql_correct"] is None
    assert report["results"][0]["input_tokens"] is None
    assert report["results"][0]["output_tokens"] is None
    assert report["results"][0]["estimated_cost"] is None
    assert report["results"][0]["error_category"] == "model_error"
    assert report["results"][1]["case_id"] == "S03"
    assert report["results"][1]["sql_correct"] is False


def test_case_and_total_timeouts_stop_without_repeating_finished_work(shopbench, tmp_path):
    database_dir, manifest = shopbench
    slow = FakeModel(sleep_first=0.2)
    report = _run(tmp_path, database_dir, manifest, slow, max_cases=2, case_timeout_seconds=0.05)
    assert report["stopped_reason"] == "Case timeout reached"
    assert [row["case_id"] for row in report["results"]] == ["S01"]
    assert report["results"][0]["error_category"] == "case_timeout"
    assert report["results"][0]["predicted_sql"] is None
    assert report["accuracy_denominator"] == 0
    assert report["sql_accuracy"] is None
    assert slow.calls == 1
    fresh = FakeModel()
    again = _run(tmp_path / "resume-timeout", database_dir, manifest, fresh, max_cases=1,
                 case_timeout_seconds=0.05)
    follow = FakeModel()
    resumed = run_pilot(
        tmp_path / "pilot", database_dir, model=follow, expected_hashes=_hashes(manifest),
        model_id="fake-script", model_version=None, live_model=False, max_cases=2,
        case_timeout_seconds=5, total_timeout_seconds=30, **BUDGET,
    )
    assert [row["case_id"] for row in resumed["results"]] == ["S01", "S03"]
    assert follow.calls >= 1
    assert any("数据库共有多少客户" in message for message in follow.messages)
    assert resumed["results"][0]["sql_correct"] is True
    assert resumed["accuracy_denominator"] == 2
    assert again["results"][0]["case_id"] == "S01"

    limited = FakeModel(sleep_first=0.15)
    total = _run(tmp_path / "total", database_dir, manifest, limited, max_cases=2,
                 case_timeout_seconds=5, total_timeout_seconds=0.05)
    assert total["stopped_reason"] == "Total runtime limit reached"
    assert [row["case_id"] for row in total["results"]] == ["S01"]
    assert limited.calls == 1


def test_context_agreement_and_live_preflight_do_not_chat():
    assert DEFAULT_RUNTIME_NUM_CTX == 32768
    require_context_agreement(actual_context_length=262144, context_window=DEFAULT_RUNTIME_NUM_CTX,
                              input_budget=24_000, output_reserve=1_000)
    with pytest.raises(PilotRefused, match="exceeds"):
        require_context_agreement(actual_context_length=8192, context_window=DEFAULT_RUNTIME_NUM_CTX,
                                  input_budget=24_000, output_reserve=1_000)
    with pytest.raises(PilotRefused, match="do not fit"):
        require_context_agreement(actual_context_length=262144, context_window=8192,
                                  input_budget=24_000, output_reserve=1_000)
    assert context_length_from_show({"model_info": {"qwen3.context_length": 262144}}) == 262144
    assert context_length_from_show({"model_info": {}}) is None

    calls = []
    chat = {}

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{
                "name": "qwen3:4b-instruct", "digest": "digest-from-server",
                "details": {"format": "gguf"},
            }]})
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"model_info": {"qwen3.context_length": 262144}})
        chat["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "done": True, "done_reason": "stop", "message": {"content": "ok"},
            "prompt_eval_count": 3, "eval_count": 1,
        })

    with httpx.Client(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(respond)) as client:
        model, _version, declared = prepare_live_model(
            client, model="qwen3:4b-instruct", context_window=DEFAULT_RUNTIME_NUM_CTX,
            input_budget=24_000, output_reserve=1_000)
        assert declared == 262144
        assert model.context_window == DEFAULT_RUNTIME_NUM_CTX
        assert calls == ["/api/tags", "/api/show"]
        model(role="decomposer", model="qwen3:4b-instruct", messages=[{"role": "user", "content": "ok"}],
              max_output_tokens=16)
    assert chat["body"]["options"]["num_ctx"] == DEFAULT_RUNTIME_NUM_CTX
    with pytest.raises(PilotRefused, match="exceeds"):
        with httpx.Client(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": [{
                "name": "qwen3:4b-instruct", "details": {"format": "gguf"},
            }]} if request.url.path == "/api/tags" else {"model_info": {"qwen3.context_length": 8192}})
        )) as client:
            prepare_live_model(client, model="qwen3:4b-instruct", context_window=DEFAULT_RUNTIME_NUM_CTX,
                               input_budget=24_000, output_reserve=1_000)
    with pytest.raises(PilotRefused, match="Cloud"):
        prepare_live_model(client, model="qwen3:cloud", context_window=DEFAULT_RUNTIME_NUM_CTX,
                           input_budget=24_000, output_reserve=1_000)


def test_case_subset_runs_only_the_requested_ids(shopbench, tmp_path):
    database_dir, manifest = shopbench
    model = FakeModel()
    report = _run(tmp_path, database_dir, manifest, model, max_cases=12, selected_case_ids=("S16", "S01"))
    assert [row["case_id"] for row in report["results"]] == ["S01", "S16"]
    assert all("上海有多少客户" not in message for message in model.messages)
    saved = json.loads((tmp_path / "pilot" / "manifest.json").read_text(encoding="utf-8"))
    assert saved["selected_case_ids"] == ["S01", "S16"]
    assert saved["num_ctx"] == WINDOW
    with pytest.raises(PilotRefused, match="subset"):
        _run(tmp_path / "bad", database_dir, manifest, model, selected_case_ids=("S99",))


def test_unscored_failure_is_retried_and_a_different_window_stays_separate(shopbench, tmp_path):
    database_dir, manifest = shopbench
    failed = FakeModel(fail_on=1)
    first = _run(
        tmp_path, database_dir, manifest, failed, max_cases=1,
        actual_context_length=262144, context_window=262144,
    )
    assert first["results"][0]["case_id"] == "S01"
    assert first["results"][0]["predicted_sql"] is None
    assert first["results"][0]["sql_correct"] is None
    assert first["accuracy_denominator"] == 0
    assert first["sql_accuracy"] is None
    assert first["real_agent_evaluation"] == "not_run"
    oom = {
        "case_id": "S01", "terminal": True, "evaluation_status": "evaluation_error",
        "predicted_sql": None, "sql_correct": None, "error_category": "out_of_memory",
    }
    assert _is_scored(oom) is False
    assert _evaluation_outcome([oom], ("S01",), live_model=True, stopped=None) == "failed"

    retry = FakeModel()
    second = _run(
        tmp_path, database_dir, manifest, retry, max_cases=1,
        actual_context_length=262144, context_window=262144,
    )
    assert retry.calls == 1
    assert second["results"][0]["sql_correct"] is True
    assert second["accuracy_denominator"] == 1
    assert second["sql_accuracy"] == 1.0
    journal = (tmp_path / "pilot" / "results.jsonl").read_text(encoding="utf-8")
    saved = json.loads((tmp_path / "pilot" / "manifest.json").read_text(encoding="utf-8"))
    assert saved["declared_context_length"] == 262144
    assert saved["num_ctx"] == 262144

    separate = FakeModel()
    with pytest.raises(PilotRefused, match="does not match"):
        _run(
            tmp_path, database_dir, manifest, separate, max_cases=1,
            actual_context_length=262144, context_window=DEFAULT_RUNTIME_NUM_CTX,
        )
    assert separate.calls == 0
    assert (tmp_path / "pilot" / "results.jsonl").read_text(encoding="utf-8") == journal


def test_cli_refuses_without_explicit_authorization(tmp_path):
    with pytest.raises(SystemExit, match="refused"):
        main(["--database-dir", str(tmp_path), "--output-dir", str(tmp_path / "out"),
              "--model", "qwen3:4b-instruct", "--context-window", "32768"])
