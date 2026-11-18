"""Run the existing three-role graph with a scripted model. Gold stays outside the agent."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
import time

from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent
from sql_agent_lab.macsql.reference_core import ModelReply, ROLES, WorkflowConfig

from ..graders.sql_grader import SCORER, grade_sql


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "offline_agent_scripts.json"
TOOL_EVENTS = {"schema_loaded", "sql_validation", "execution", "schema_refreshed"}


class ScriptedRoleModel:
    """Fixed role replies. It records the prompt it actually received."""

    def __init__(self, responses: dict, input_tokens, output_tokens):
        self.responses = responses
        self.reply_input_tokens = 0 if input_tokens is None else input_tokens
        self.reply_output_tokens = 0 if output_tokens is None else output_tokens
        self.declared_input_tokens = input_tokens
        self.declared_output_tokens = output_tokens
        self.calls = []

    def __call__(self, *, role, model, messages, max_output_tokens):
        self.calls.append({
            "role": role,
            "model": model,
            "messages": [{"role": item["role"], "content": item["content"]} for item in messages],
            "max_output_tokens": max_output_tokens,
        })
        if role not in self.responses:
            raise KeyError(f"Script has no reply for {role}")
        return ModelReply(
            self.responses[role],
            self.reply_input_tokens,
            self.reply_output_tokens,
            simulated=True,
            response_id="offline-script",
        )


def load_scripts(path: Path = SCRIPT_PATH) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _error_category(execution_status: str, grade: dict):
    if grade["status"] == "evaluation_error":
        return "evaluation_error"
    if grade["status"] == "timeout":
        return "timeout"
    if grade.get("sql_correct") is True:
        return None
    if execution_status == "error":
        return "execution_rejected"
    return grade.get("reason") or "result_mismatch"


def run_scripted_case(script: dict, tools) -> dict:
    """Execute one script through ReferenceMacSQLAgent. This function accepts no gold SQL."""
    if "sql" in script or "gold" in script:
        raise ValueError("Offline script must not carry gold SQL")
    model = ScriptedRoleModel(script["responses"], script.get("input_tokens"), script.get("output_tokens"))
    events = []
    agent = ReferenceMacSQLAgent(
        {role: "scripted-offline" for role in ROLES},
        config=WorkflowConfig(
            tools,
            model,
            trace=events.append,
            semantic_review=False,
            schema_retrieval=False,
            max_repairs=1,
        ),
    )
    question = Question(script["script_id"], script["source_id"], script["question"])
    started = time.perf_counter()
    try:
        predicted_sql = agent.run(question)
        run_error = None
    except Exception as error:
        predicted_sql = None
        run_error = error
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    completed = next((event for event in reversed(events) if event.get("event") == "workflow_completed"), {})
    execution_status = completed.get("status")
    if run_error is not None:
        execution_status = "error"
    return {
        "script_id": script["script_id"],
        "case_id": script["case_id"],
        "datasource_id": script["source_id"],
        "question": script["question"],
        "predicted_sql": predicted_sql,
        "sql_execution_status": execution_status,
        "role_call_counts": dict(Counter(call["role"] for call in model.calls)),
        "roles": [call["role"] for call in model.calls],
        "tool_calls": sum(1 for event in events if event.get("event") in TOOL_EVENTS),
        "repair_count": completed.get("repair_count", 0),
        "latency_ms": elapsed_ms,
        "input_tokens": model.declared_input_tokens,
        "output_tokens": model.declared_output_tokens,
        "model_inputs": [call["messages"] for call in model.calls],
        "trace_events": [event.get("event") for event in events],
        "semantic_review": False,
        "run_error": None if run_error is None else f"{type(run_error).__name__}: {run_error}",
    }


def grade_prediction(execution: dict, tools, gold: dict) -> dict:
    """Score after the agent returns. Gold is not passed back into the workflow."""
    if execution["predicted_sql"] is None:
        grade = {"status": "evaluation_error", "sql_correct": None, "reason": "agent_run_failed", "scorer": SCORER}
    else:
        grade = grade_sql(
            tools,
            execution["datasource_id"],
            gold["sql"],
            execution["predicted_sql"],
            order_sensitive=gold["order_sensitive"],
            numeric_tolerance=gold["numeric_tolerance"],
        )
    public = {key: value for key, value in grade.items() if key not in {"gold_rows", "predicted_rows"}}
    return {
        "case_id": execution["case_id"],
        "script_id": execution["script_id"],
        "datasource_id": execution["datasource_id"],
        "predicted_sql": execution["predicted_sql"],
        "sql_execution_status": execution["sql_execution_status"],
        "sql_correct": public.get("sql_correct"),
        "role_call_counts": execution["role_call_counts"],
        "tool_calls": execution["tool_calls"],
        "repair_count": execution["repair_count"],
        "latency_ms": execution["latency_ms"],
        "input_tokens": execution["input_tokens"],
        "output_tokens": execution["output_tokens"],
        "evaluation_status": public.get("status"),
        "error_category": _error_category(execution["sql_execution_status"], public),
        "scorer": SCORER,
        "real_agent_evaluation": "not_run",
        "is_agent_result": False,
    }
