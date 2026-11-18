"""Explicit local Ollama pilot for a fixed ShopBench slice.

The default command refuses to call a model. Fixture evaluation stays in evals.runner.
Gold SQL is loaded only after the agent returns, for shopbench_strict_v1.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import argparse
import hashlib
import json
import time

from sql_agent_lab.macsql.context import ContextPolicy
from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent
from sql_agent_lab.macsql.reference_core import ROLES, WorkflowConfig
from sql_agent_lab.macsql.run_local import LocalOllamaModel
from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits

from ..graders.sql_grader import SCORER, grade_sql
from ..runner import load_jsonl


SOURCE_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = SOURCE_ROOT / "evals" / "cases" / "single_turn.jsonl"
GOLD_PATH = SOURCE_ROOT / "evals" / "gold" / "single_turn_gold.jsonl"
PROMPT_FILES = (
    SOURCE_ROOT / "src" / "sql_agent_lab" / "macsql" / "reference_baseline.py",
    SOURCE_ROOT / "src" / "sql_agent_lab" / "macsql" / "result_risks.py",
)
TOOL_EVENTS = {"schema_loaded", "sql_validation", "execution", "schema_refreshed"}
COMPLEX_OUTPUT_TOKENS = 2400
SAFETY_MARGIN = 512
TEMPERATURE = 0
MODEL_SEED = 123
MAX_CALLS_PER_CASE = 3
DEFAULT_RUNTIME_NUM_CTX = 32_768

# Frozen slice. Questions are read from the ShopBench case file, never from gold.
PILOT_CASES = (
    ("S01", "simple_query"),
    ("S03", "simple_filter"),
    ("S05", "aggregation_join"),
    ("S10", "aggregation"),
    ("S14", "payment_aggregate"),
    ("S16", "net_receipts"),
    ("S17", "join"),
    ("S20", "unmatched_customers"),
    ("S25", "repeated_payments"),
    ("S26", "repeated_refunds"),
    ("S33", "null"),
    ("S37", "empty_result"),
)
PILOT_CASE_IDS = tuple(case_id for case_id, _coverage in PILOT_CASES)
MAX_CASES = len(PILOT_CASE_IDS)
MAX_MODEL_CALLS = MAX_CASES * MAX_CALLS_PER_CASE


class PilotRefused(RuntimeError):
    """The pilot stopped before or during a run without calling a forbidden model."""


def prompt_config_hash(*, context_window: int, input_budget: int, output_reserve: int) -> str:
    digest = hashlib.sha256()
    for path in PROMPT_FILES:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    payload = {
        "complex_output_tokens": COMPLEX_OUTPUT_TOKENS,
        "context_window": context_window,
        "input_budget": input_budget,
        "max_repairs": 1,
        "model_seed": MODEL_SEED,
        "output_reserve": output_reserve,
        "safety_margin": SAFETY_MARGIN,
        "schema_retrieval": False,
        "scorer": SCORER,
        "semantic_review": False,
        "temperature": TEMPERATURE,
    }
    digest.update(json.dumps(payload, sort_keys=True).encode())
    return digest.hexdigest()


def context_length_from_show(payload: dict) -> int | None:
    info = payload.get("model_info") or {}
    found = []
    for key, value in info.items():
        if key == "context_length" or str(key).endswith(".context_length"):
            found.append(value)
    if len(found) != 1 or type(found[0]) is not int or found[0] <= 0:
        return None
    return found[0]


def require_context_agreement(*, actual_context_length, context_window: int,
                              input_budget: int, output_reserve: int) -> None:
    """actual_context_length is the model maximum. context_window is the runtime num_ctx."""
    if actual_context_length is None:
        raise PilotRefused("The model did not report one context length")
    if context_window > actual_context_length:
        raise PilotRefused("Runtime num_ctx exceeds the model's declared context length")
    if input_budget + COMPLEX_OUTPUT_TOKENS + SAFETY_MARGIN > context_window:
        raise PilotRefused("Input budget and maximum SQL output do not fit the runtime num_ctx")
    try:
        ContextPolicy(
            input_budget=input_budget,
            output_reserve=output_reserve,
            context_window=context_window,
            safety_margin=SAFETY_MARGIN,
        )
    except ValueError as error:
        raise PilotRefused(str(error)) from error


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hashes_from_seed_manifest(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["source_id"]: item["file_sha256"] for item in payload["databases"]}


def select_case_ids(raw: str | None) -> tuple[str, ...]:
    """A subset of the frozen pilot list. None keeps the full list, in frozen order."""
    if raw is None or not str(raw).strip():
        return PILOT_CASE_IDS
    requested = tuple(part.strip() for part in str(raw).split(",") if part.strip())
    unknown = [case_id for case_id in requested if case_id not in PILOT_CASE_IDS]
    if unknown or len(requested) != len(set(requested)):
        raise PilotRefused("Pilot cases must be a unique subset of the frozen 12")
    return tuple(case_id for case_id in PILOT_CASE_IDS if case_id in set(requested))


def load_pilot_questions() -> list[dict]:
    rows = {row["case_id"]: row for row in load_jsonl(CASES_PATH)}
    selected = []
    for case_id, coverage in PILOT_CASES:
        row = rows.get(case_id)
        if row is None:
            raise PilotRefused(f"Fixed pilot case {case_id} is missing")
        if "sql" in row or "gold" in row:
            raise PilotRefused(f"{case_id} exposes SQL in the model-visible case file")
        selected.append({
            "case_id": row["case_id"],
            "source_id": row["source_id"],
            "question": row["question"],
            "coverage": coverage,
        })
    return selected


def _load_gold() -> dict[str, dict]:
    return {row["case_id"]: row for row in load_jsonl(GOLD_PATH)}


def _output_allowed(path: Path) -> None:
    resolved = path.resolve()
    root = SOURCE_ROOT.resolve()
    blocked = [root / "src", root / "evals", root / "tests", root / "docs"]
    if resolved == root or any(resolved == item or item in resolved.parents for item in blocked):
        raise PilotRefused("Write the pilot journal under .local or outside the source tree")


def _verify_databases(database_dir: Path, expected: dict[str, str], sources: set[str]) -> dict[str, Path]:
    missing = sources - set(expected)
    if missing:
        raise PilotRefused("Database manifest has no hash for " + ", ".join(sorted(missing)))
    paths = {}
    for source_id in sorted(sources):
        path = database_dir / f"{source_id}.sqlite"
        if not path.is_file():
            raise PilotRefused(f"Database file for {source_id} is missing")
        actual = file_sha256(path)
        if actual != expected[source_id]:
            raise PilotRefused(f"Database hash mismatch for {source_id}")
        paths[source_id] = path
    return paths


class _CallBudget:
    """Counts model calls and keeps the prompt text for the gold-isolation check."""

    def __init__(self, inner, *, max_model_calls: int, case_timeout: float, total_timeout: float, started: float):
        self.inner = inner
        self.max_model_calls = max_model_calls
        self.case_timeout = case_timeout
        self.total_timeout = total_timeout
        self.started = started
        self.calls = 0
        self.case_calls = 0
        self.case_messages: list = []
        self.case_roles: list[str] = []
        self.usage_complete = True
        self.input_tokens = 0
        self.output_tokens = 0
        self.case_started = started

    def begin_case(self) -> None:
        self.case_calls = 0
        self.case_messages = []
        self.case_roles = []
        self.usage_complete = True
        self.input_tokens = 0
        self.output_tokens = 0
        now = time.monotonic()
        if now - self.started > self.total_timeout:
            raise PilotRefused("Total runtime limit reached")
        self.case_started = now

    def __call__(self, *, role, model, messages, max_output_tokens):
        now = time.monotonic()
        if now - self.started > self.total_timeout:
            raise PilotRefused("Total runtime limit reached")
        if now - self.case_started > self.case_timeout:
            raise PilotRefused("Case timeout reached")
        if self.calls >= self.max_model_calls:
            raise PilotRefused("Model call budget reached")
        self.calls += 1
        self.case_calls += 1
        self.case_roles.append(role)
        self.case_messages.append([{"role": item["role"], "content": item["content"]} for item in messages])
        try:
            reply = self.inner(role=role, model=model, messages=messages, max_output_tokens=max_output_tokens)
        except Exception:
            self.usage_complete = False
            raise
        if time.monotonic() - self.case_started > self.case_timeout:
            self.usage_complete = False
            raise PilotRefused("Case timeout reached")
        if reply.input_tokens is None or reply.output_tokens is None:
            self.usage_complete = False
        else:
            self.input_tokens += reply.input_tokens
            self.output_tokens += reply.output_tokens
        return reply


def _append_jsonl(path: Path, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()


def _json_safe(value):
    """Trace events must survive JSON even when an HTTP error carries a Response object."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _is_scored(row: dict) -> bool:
    """Only an executed SQL prediction can enter the accuracy denominator."""
    return (
        row.get("evaluation_status") == "scored"
        and bool(row.get("predicted_sql"))
        and row.get("sql_correct") is not None
    )


def _latest_results(rows: list[dict]) -> list[dict]:
    latest = {}
    order = []
    for row in rows:
        case_id = row["case_id"]
        if case_id not in latest:
            order.append(case_id)
        latest[case_id] = row
    return [latest[case_id] for case_id in order]


def _failure_category(error: Exception) -> str:
    text = str(error).lower()
    if "out-of-memory" in text or "out of memory" in text or "kv cache" in text:
        return "out_of_memory"
    return "model_error"


def _evaluation_outcome(rows: list[dict], selected: tuple[str, ...], *, live_model: bool, stopped) -> str:
    if not live_model:
        return "not_run"
    latest = {row["case_id"]: row for row in _latest_results(rows)}
    if stopped is None and all(_is_scored(latest.get(case_id, {})) for case_id in selected):
        return "completed"
    if any(case_id in latest and not _is_scored(latest[case_id]) for case_id in selected):
        return "failed"
    return "incomplete"


def _read_journal(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise PilotRefused(f"Pilot journal line {number} is unreadable") from error
        if row.get("case_id") not in PILOT_CASE_IDS or row.get("terminal") is not True:
            raise PilotRefused(f"Pilot journal line {number} is not a finished pilot case")
        rows.append(row)
    scored_ids = [row["case_id"] for row in rows if _is_scored(row)]
    if len(scored_ids) != len(set(scored_ids)):
        raise PilotRefused("Pilot journal contains a duplicate scored case")
    return rows


def _identity(manifest: dict) -> dict:
    keys = (
        "case_ids", "database_sha256", "model_id", "model_version", "prompt_config_hash",
        "declared_context_length", "context_window", "num_ctx", "input_budget", "output_reserve",
        "temperature", "scorer",
    )
    return {key: manifest[key] for key in keys}


def _public_case(case_id: str, source_id: str, coverage: str, *, predicted_sql, execution_status,
                  grade: dict, roles: list[str], tool_calls: int, repair_count: int, latency_ms: int,
                  input_tokens, output_tokens, error_category, result_sha256) -> dict:
    return {
        "terminal": True,
        "case_id": case_id,
        "coverage": coverage,
        "datasource_id": source_id,
        "predicted_sql": predicted_sql,
        "sql_execution_status": execution_status,
        "sql_correct": grade.get("sql_correct"),
        "role_call_counts": dict(Counter(roles)),
        "tool_calls": tool_calls,
        "repair_count": repair_count,
        "latency_ms": latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost": None,
        "evaluation_status": grade.get("status"),
        "error_category": error_category,
        "predicted_result_sha256": result_sha256,
        "scorer": SCORER,
    }


def _grade(tools, source_id: str, gold: dict, predicted_sql: str) -> tuple[dict, str | None]:
    grade = grade_sql(
        tools, source_id, gold["sql"], predicted_sql,
        order_sensitive=gold["order_sensitive"], numeric_tolerance=gold["numeric_tolerance"],
    )
    digest = None
    predicted = grade.get("predicted_rows")
    if isinstance(predicted, dict) and predicted.get("status") == "ok":
        encoded = json.dumps(predicted["rows"], ensure_ascii=False, default=str).encode()
        digest = hashlib.sha256(encoded).hexdigest()
    public = {key: value for key, value in grade.items() if key not in {"gold_rows", "predicted_rows"}}
    return public, digest


def _error_category(execution_status, grade: dict):
    if grade.get("sql_correct") is True:
        return None
    if grade.get("status") == "evaluation_error":
        return "evaluation_error"
    if grade.get("status") == "timeout":
        return "timeout"
    if execution_status == "error":
        return "execution_rejected"
    return grade.get("reason") or "result_mismatch"


def _assert_gold_hidden(messages, gold_sql: str) -> None:
    visible = json.dumps(messages, ensure_ascii=False)
    if gold_sql and gold_sql in visible:
        raise PilotRefused("Gold SQL was visible to the model")


def _write_summary(path: Path, manifest: dict, rows: list[dict], stopped_reason: str | None) -> None:
    rows = _latest_results(rows)
    scored = [row for row in rows if _is_scored(row)]
    correct = sum(1 for row in scored if row.get("sql_correct") is True)
    lines = [
        "# ShopBench local pilot",
        "",
        f"- real_agent_evaluation: `{manifest['real_agent_evaluation']}`",
        f"- model_id: `{manifest['model_id']}`",
        f"- model_version: `{manifest['model_version']}`",
        f"- prompt_config_hash: `{manifest['prompt_config_hash']}`",
        f"- declared_context_length: {manifest['declared_context_length']}",
        f"- runtime_num_ctx: {manifest['num_ctx']}",
        f"- input_budget: {manifest['input_budget']}",
        f"- output_reserve: {manifest['output_reserve']}",
        f"- complex_output_tokens: {COMPLEX_OUTPUT_TOKENS}",
        f"- temperature: {TEMPERATURE}",
        f"- scorer: `{SCORER}`",
        f"- finished_cases: {len(rows)}",
        f"- accuracy_denominator: {len(scored)}",
        f"- scored_correct: {correct}",
        f"- sql_accuracy: {manifest.get('sql_accuracy')}",
        f"- estimated_cost: null",
        f"- stopped_reason: {stopped_reason}",
        "",
        "| case_id | source | coverage | execution | sql_correct | calls | repair | error_category |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        calls = sum(row.get("role_call_counts", {}).values())
        lines.append(
            f"| {row['case_id']} | {row['datasource_id']} | {row['coverage']} | "
            f"{row['sql_execution_status']} | {row['sql_correct']} | {calls} | "
            f"{row['repair_count']} | {row['error_category']} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _manifest(*, model_id, model_version, actual_context_length, context_window, input_budget,
              output_reserve, database_sha256, max_cases, max_model_calls, case_timeout,
              total_timeout, live_model) -> dict:
    return {
        "pilot": "shopbench_v1_local_ollama",
        "real_agent_evaluation": "not_run" if not live_model else "authorized_not_recorded_until_finish",
        "live_model": live_model,
        "cloud_api_calls": 0,
        "dataset_version": "shopbench_v1",
        "case_ids": list(PILOT_CASE_IDS),
        "coverage": {case_id: coverage for case_id, coverage in PILOT_CASES},
        "database_sha256": database_sha256,
        "model_id": model_id,
        "model_version": model_version,
        "prompt_config_hash": prompt_config_hash(
            context_window=context_window, input_budget=input_budget, output_reserve=output_reserve),
        "declared_context_length": actual_context_length,
        "context_window": context_window,
        "actual_context_length": actual_context_length,
        "num_ctx": context_window,
        "runtime_num_ctx": context_window,
        "input_budget": input_budget,
        "output_reserve": output_reserve,
        "complex_output_tokens": COMPLEX_OUTPUT_TOKENS,
        "safety_margin": SAFETY_MARGIN,
        "temperature": TEMPERATURE,
        "model_seed": MODEL_SEED,
        "scorer": SCORER,
        "semantic_review": False,
        "schema_retrieval": False,
        "max_repairs": 1,
        "max_cases": max_cases,
        "max_model_calls": max_model_calls,
        "case_timeout_seconds": case_timeout,
        "total_timeout_seconds": total_timeout,
        "estimated_cost": None,
    }


def prepare_live_model(client, *, model: str, context_window: int, input_budget: int, output_reserve: int):
    """Reuse LocalOllamaModel. Chat is not called here."""
    if model.endswith((":cloud", "-cloud")):
        raise PilotRefused("Cloud model names are not allowed")
    host = client.base_url.host
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise PilotRefused("Ollama must be on loopback")
    tap_payload = {}

    class _Tap:
        def __init__(self, inner):
            self._inner = inner
            self.base_url = inner.base_url

        def get(self, *args, **kwargs):
            return self._inner.get(*args, **kwargs)

        def post(self, url, **kwargs):
            response = self._inner.post(url, **kwargs)
            if url == "/api/show":
                tap_payload["show"] = response.json()
            return response

    try:
        adapted = LocalOllamaModel(_Tap(client), model, context_window=context_window)
    except ValueError as error:
        raise PilotRefused(str(error)) from error
    actual = context_length_from_show(tap_payload.get("show") or {})
    require_context_agreement(
        actual_context_length=actual,
        context_window=context_window,
        input_budget=input_budget,
        output_reserve=output_reserve,
    )
    version = adapted.metadata.get("digest")
    if version is not None and not isinstance(version, str):
        version = None
    return adapted, version, actual


def run_pilot(output_dir: Path, database_dir: Path, *, model, expected_hashes: dict[str, str],
              model_id: str, model_version: str | None, actual_context_length: int, context_window: int,
              input_budget: int = 24_000, output_reserve: int = 1_000, max_cases: int = MAX_CASES,
              max_model_calls: int = MAX_MODEL_CALLS, case_timeout_seconds: float = 180,
              total_timeout_seconds: float = 1800, live_model: bool = False,
              selected_case_ids: tuple[str, ...] | None = None) -> dict:
    if live_model and not isinstance(model, LocalOllamaModel):
        raise PilotRefused("A live pilot requires LocalOllamaModel")
    if not live_model and isinstance(model, LocalOllamaModel):
        raise PilotRefused("Offline validation cannot use the live Ollama adapter")
    if model is None:
        raise PilotRefused("A scripted or fake model is required for offline validation")
    if not 1 <= max_cases <= MAX_CASES or not 1 <= max_model_calls <= MAX_MODEL_CALLS:
        raise PilotRefused("Case and model-call budgets exceed the pilot cap")
    if case_timeout_seconds <= 0 or total_timeout_seconds <= 0:
        raise PilotRefused("Timeouts must be positive")
    if TEMPERATURE != 0:
        raise PilotRefused("The reused local adapter uses temperature 0")
    require_context_agreement(
        actual_context_length=actual_context_length,
        context_window=context_window,
        input_budget=input_budget,
        output_reserve=output_reserve,
    )
    selected = select_case_ids(",".join(selected_case_ids) if selected_case_ids else None)
    questions = [item for item in load_pilot_questions() if item["case_id"] in set(selected)]
    sources = {item["source_id"] for item in questions}
    paths = _verify_databases(database_dir, expected_hashes, sources)
    _output_allowed(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest(
        model_id=model_id, model_version=model_version, actual_context_length=actual_context_length,
        context_window=context_window, input_budget=input_budget, output_reserve=output_reserve,
        database_sha256={source: expected_hashes[source] for source in sorted(sources)},
        max_cases=max_cases, max_model_calls=max_model_calls, case_timeout=case_timeout_seconds,
        total_timeout=total_timeout_seconds, live_model=live_model,
    )
    manifest["selected_case_ids"] = list(selected)
    manifest["num_ctx"] = context_window
    manifest_path = output_dir / "manifest.json"
    journal_path = output_dir / "results.jsonl"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if _identity(previous) != _identity(manifest):
            raise PilotRefused("Existing pilot manifest does not match this run")
    else:
        if any(output_dir.iterdir()):
            raise PilotRefused("Output directory is not an empty pilot directory")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if isinstance(model, LocalOllamaModel) and model.context_window != context_window:
        raise PilotRefused("Ollama num_ctx does not match the configured runtime window")
    finished = _read_journal(journal_path)
    done = {row["case_id"] for row in finished if _is_scored(row)}
    already_scored = len(done)
    attempted_now = 0
    gold_index = _load_gold()
    tools = SQLiteTools(paths, ToolLimits(timeout_seconds=30))
    started = time.monotonic()
    budget = _CallBudget(
        model, max_model_calls=max_model_calls, case_timeout=case_timeout_seconds,
        total_timeout=total_timeout_seconds, started=started,
    )
    prior_calls = sum(sum(row.get("role_call_counts", {}).values()) for row in finished)
    budget.calls = prior_calls
    calls_before = prior_calls
    stopped = None
    for item in questions:
        if item["case_id"] in done:
            continue
        if already_scored + attempted_now >= max_cases:
            stopped = "max_cases"
            break
        attempted_now += 1
        events = []
        trace_path = output_dir / "trace.jsonl"

        def persist(event, case_id=item["case_id"]):
            event = _json_safe({"case_id": case_id, **event})
            events.append(event)
            _append_jsonl(trace_path, event)

        case_started = time.perf_counter()
        predicted = None
        run_error = None
        try:
            budget.begin_case()
            agent = ReferenceMacSQLAgent(
                {role: model_id for role in ROLES},
                config=WorkflowConfig(
                    tools, budget, context=ContextPolicy(
                        input_budget=input_budget, output_reserve=output_reserve,
                        context_window=context_window, safety_margin=SAFETY_MARGIN,
                    ),
                    trace=persist, semantic_review=False, schema_retrieval=False,
                    max_repairs=1, complex_output_tokens=COMPLEX_OUTPUT_TOKENS,
                ),
            )
            question = Question(item["case_id"], item["source_id"], item["question"])
            predicted = agent.run(question)
        except PilotRefused as error:
            if budget.case_calls:
                reason = str(error)
                category = "case_timeout" if "timeout" in reason.lower() else "budget_exceeded"
                status = "timeout" if category == "case_timeout" else "not_scored"
                record = _public_case(
                    item["case_id"], item["source_id"], item["coverage"], predicted_sql=None,
                    execution_status="error", grade={"status": status, "sql_correct": None},
                    roles=budget.case_roles, tool_calls=0, repair_count=0,
                    latency_ms=int((time.perf_counter() - case_started) * 1000),
                    input_tokens=None, output_tokens=None, error_category=category, result_sha256=None,
                )
                _append_jsonl(journal_path, record)
                finished.append(record)
                done.add(item["case_id"])
            stopped = str(error)
            break
        except Exception as error:
            predicted = None
            run_error = error
        latency_ms = int((time.perf_counter() - case_started) * 1000)
        gold = gold_index[item["case_id"]]
        try:
            _assert_gold_hidden(budget.case_messages, gold["sql"])
        except PilotRefused:
            record = _public_case(
                item["case_id"], item["source_id"], item["coverage"], predicted_sql=None,
                execution_status="error", grade={"status": "evaluation_error", "sql_correct": None},
                roles=budget.case_roles, tool_calls=0, repair_count=0, latency_ms=latency_ms,
                input_tokens=None, output_tokens=None, error_category="gold_leak", result_sha256=None,
            )
            _append_jsonl(journal_path, record)
            finished.append(record)
            stopped = "gold_leak"
            break
        if any(event.get("event") == "schema_retrieved" for event in events):
            raise PilotRefused("Schema retrieval was enabled")
        completed = next((event for event in reversed(events) if event.get("event") == "workflow_completed"), {})
        execution_status = "error" if run_error is not None else completed.get("status")
        if predicted is None:
            grade = {"status": "evaluation_error", "sql_correct": None, "reason": "agent_run_failed"}
            digest = None
            category = _failure_category(run_error) if run_error is not None else "evaluation_error"
        else:
            grade, digest = _grade(tools, item["source_id"], gold, predicted)
            category = _error_category(execution_status, grade)
        tokens_in = budget.input_tokens if budget.usage_complete and run_error is None else None
        tokens_out = budget.output_tokens if budget.usage_complete and run_error is None else None
        record = _public_case(
            item["case_id"], item["source_id"], item["coverage"], predicted_sql=predicted,
            execution_status=execution_status, grade=grade, roles=budget.case_roles,
            tool_calls=sum(1 for event in events if event.get("event") in TOOL_EVENTS),
            repair_count=completed.get("repair_count", 0), latency_ms=latency_ms,
            input_tokens=tokens_in, output_tokens=tokens_out, error_category=category,
            result_sha256=digest,
        )
        _append_jsonl(journal_path, record)
        finished.append(record)
        if _is_scored(record):
            done.add(item["case_id"])
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key in ("reused_case_ids", "reused_from", "run_label"):
            if key in previous:
                manifest[key] = previous[key]
    latest = _latest_results(finished)
    scored = [row for row in latest if _is_scored(row)]
    manifest["real_agent_evaluation"] = _evaluation_outcome(
        finished, selected, live_model=live_model, stopped=stopped)
    manifest["accuracy_denominator"] = len(scored)
    manifest["sql_accuracy"] = (
        None if not scored else sum(1 for row in scored if row.get("sql_correct") is True) / len(scored)
    )
    if live_model or manifest_path.exists():
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_summary(output_dir / "summary.md", manifest, latest, stopped)
    return {
        "manifest": manifest,
        "results": latest,
        "stopped_reason": stopped,
        "model_calls": budget.calls - calls_before,
        "bound_sources": sorted(paths),
        "real_agent_evaluation": manifest["real_agent_evaluation"],
        "sql_accuracy": manifest["sql_accuracy"],
        "accuracy_denominator": manifest["accuracy_denominator"],
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="ShopBench local Ollama pilot. Refuses unless explicitly authorized.")
    parser.add_argument("--authorize-local-ollama", action="store_true")
    parser.add_argument("--model", default=None)
    parser.add_argument("--database-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--context-window", type=int, default=DEFAULT_RUNTIME_NUM_CTX)
    parser.add_argument("--input-budget", type=int, default=24_000)
    parser.add_argument("--output-reserve", type=int, default=1_000)
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--max-cases", type=int, default=MAX_CASES)
    parser.add_argument("--max-model-calls", type=int, default=MAX_MODEL_CALLS)
    parser.add_argument("--case-timeout", type=float, default=180)
    parser.add_argument("--total-timeout", type=float, default=1800)
    parser.add_argument("--request-timeout", type=float, default=180)
    parser.add_argument("--cases", default=None, help="Comma-separated subset of the frozen 12 case ids")
    args = parser.parse_args(argv)
    if not args.authorize_local_ollama:
        raise SystemExit("Local Ollama pilot refused: pass --authorize-local-ollama to call a loopback model")
    if not args.model:
        raise SystemExit("Local Ollama pilot refused: --model is required")
    if args.context_window <= 0:
        raise SystemExit("Local Ollama pilot refused: runtime num_ctx must be positive")
    if not 1024 <= args.port <= 65535 or args.request_timeout <= 0:
        raise SystemExit("Local Ollama pilot refused: use an unprivileged port and a positive request timeout")
    import httpx
    import os
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    expected = hashes_from_seed_manifest(args.database_dir / "manifest.generated.json")
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False,
                      follow_redirects=False, timeout=args.request_timeout) as client:
        model, version, actual = prepare_live_model(
            client, model=args.model, context_window=args.context_window,
            input_budget=args.input_budget, output_reserve=args.output_reserve,
        )
        run_pilot(
            args.output_dir, args.database_dir, model=model, expected_hashes=expected,
            model_id=args.model, model_version=version, actual_context_length=actual,
            context_window=args.context_window, input_budget=args.input_budget,
            output_reserve=args.output_reserve, max_cases=args.max_cases,
            max_model_calls=args.max_model_calls, case_timeout_seconds=args.case_timeout,
            total_timeout_seconds=args.total_timeout, live_model=True,
            selected_case_ids=select_case_ids(args.cases),
        )


if __name__ == "__main__":
    main()
