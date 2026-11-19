"""Shared execution graph and accounting for the reference-style MAC-SQL agent."""

from dataclasses import dataclass, field
import json
from pathlib import Path
from threading import Lock
from typing import Protocol, TypedDict
from uuid import uuid4

from langgraph.graph import END, StateGraph

from .context import ContextPolicy
from .dataset import Question
from .tools import SQLiteTools


ROLES = ("selector", "decomposer", "refiner")


class RoleOutputError(ValueError):
    """The model did not provide a usable role response."""


@dataclass(frozen=True)
class ModelReply:
    text: str
    input_tokens: int
    output_tokens: int
    response_id: str = ""
    simulated: bool = False
    raw_usage: dict = field(default_factory=dict)


class RoleModel(Protocol):
    def __call__(self, *, role: str, model: str, messages: list[dict],
                 max_output_tokens: int) -> ModelReply: ...


class JsonlTrace:
    """Append events from one process to a local JSONL file."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = Lock()

    def __call__(self, event):
        line = json.dumps(event, ensure_ascii=False, allow_nan=False)
        with self.lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@dataclass(frozen=True)
class WorkflowConfig:
    tools: SQLiteTools
    model: RoleModel
    context: ContextPolicy = field(default_factory=ContextPolicy)
    max_repairs: int = 1
    complex_output_tokens: int = 2400
    trace: object = None
    schema_retrieval: bool = False
    value_lookup: bool = False
    retrieval_top_k: int = 8
    semantic_review: bool = False
    semantic_suggestion_only: bool = False
    semantic_output_tokens: int = 1600
    requirements_output_tokens: int = 800
    output_type_check: bool = True
    output_types: tuple[str, ...] = ()

    def __post_init__(self):
        from .output_types import ALLOWED_TYPES
        if type(self.complex_output_tokens) is not int or not 1000 <= self.complex_output_tokens <= 3200:
            raise ValueError('Complex generation output limit must be 1000–3200 tokens')
        if (type(self.requirements_output_tokens) is not int or not 256 <= self.requirements_output_tokens <= 1600
            or self.requirements_output_tokens + self.context.safety_margin >= self.context.context_window):
            raise ValueError('Requirements output budget must be 256–1600 tokens and fit context')
        if (type(self.semantic_output_tokens) is not int or not 256 <= self.semantic_output_tokens <= 8192
                or (self.semantic_review and self.semantic_output_tokens + self.context.safety_margin >= self.context.context_window)):
            raise ValueError('Semantic output budget must be 256–8192 tokens and fit the context window')
        if any(t not in ALLOWED_TYPES for t in self.output_types):
            raise ValueError('Output types must be integer, number, text, or unknown')
        if not isinstance(self.max_repairs, int) or not 0 <= self.max_repairs <= 10:
            raise ValueError("max_repairs must be an integer between 0 and 10")
        if not isinstance(self.retrieval_top_k, int) or not 1 <= self.retrieval_top_k <= 12:
            raise ValueError('retrieval_top_k must be between 1 and 12')
        if self.value_lookup and not self.schema_retrieval:
            raise ValueError('value_lookup requires schema_retrieval')


class WorkflowState(TypedDict, total=False):
    question: Question
    run_id: str
    selected_schema: dict
    full_schema: dict
    sql: str
    feedback: dict
    repair_count: int
    schema_refresh_count: int
    coverage: dict
    plan: list
    unchanged_sql: bool
    repair_previous: dict
    repair_rejected: bool
    semantic_status: str
    retrieval_hints: str
    semantic_report: dict
    semantic_validation: dict
    semantic_metrics: dict
    semantic_review_count: int
    semantic_revision_count: int
    output_type_report: dict
    output_columns_report: dict
    output_requirements: dict
    output_requirements_digest: str
    output_requirements_count: int
    output_requirements_status: str
    output_requirements_assessment: dict
    output_requirements_protocol_warnings: list
    output_projection_suggestion: dict
    semantic_suggestion: dict


class ReferenceGraphAgent:
    """Bounded three-role graph; subclasses supply the MAC-SQL role behavior."""

    def __init__(self, models, *, config: WorkflowConfig):
        if set(models) != set(ROLES) or any(not isinstance(m, str) or not m for m in models.values()):
            raise ValueError(f"models must contain exactly {ROLES}, with model-name strings")
        self.models = dict(models)
        self.config = config
        self.grounding_block = ''
        graph = StateGraph(WorkflowState)
        graph.add_node("select_schema", self._select)
        graph.add_node("decompose", self._decompose)
        graph.add_node("execute", self._execute)
        graph.add_node("refine", self._refine)
        graph.add_node("semantic_review", self._semantic_review)
        graph.set_entry_point("select_schema")
        graph.add_edge("select_schema", "decompose")
        graph.add_edge("decompose", "execute")
        graph.add_conditional_edges("execute", self._next, {"refine": "refine", "review": "semantic_review", "end": END})
        graph.add_edge("refine", "execute")
        graph.add_edge("semantic_review", END)
        self.graph = graph.compile()

    def _event(self, state, event, **fields):
        if self.config.trace is not None:
            self.config.trace({"event": event, "run_id": state["run_id"],
                               "question_id": state["question"].question_id,
                               "models": self.models, **fields})

    def _execute(self, state):
        feedback = self.config.tools.compile_sql_readonly(state['question'].db_id, state['sql'])
        self._event(state, 'sql_validation', sql=state['sql'], feedback=feedback,
                    repair_count=state['repair_count'])
        if feedback['status'] == 'ok':
            feedback = self.config.tools.execute_sql_readonly(state['question'].db_id, state['sql'])
        type_report = self._check_output(state, state['sql'], feedback)
        feedback['output_type_check'] = type_report
        from .result_risks import inspect_result_risks
        risks = inspect_result_risks(state['sql'], feedback, type_report)
        feedback['result_risks'] = risks
        self._event(state, 'result_risks_checked', report=risks, model_call=False)
        self._event(state, 'execution', sql=state['sql'], feedback=feedback,
                    repair_count=state['repair_count'])
        previous = state.get('repair_previous')
        if previous:
            accepted = feedback['status'] == 'ok' and type_report['status'] != 'mismatch'
            reason = 'checks_passed' if accepted else 'candidate_check_failed'
            if accepted and previous['feedback']['status'] == 'ok':
                def width(value):
                    return value.get('column_count', value.get('output_type_profile', {}).get(
                        'total_columns', len(value.get('columns', []))))
                old_width = width(previous['feedback'])
                if old_width != width(feedback):
                    accepted, reason = False, 'output_shape_changed'
            self._event(state, 'repair_candidate_checked', old_sql=previous['sql'],
                        sql=state['sql'], feedback=feedback, adopted=accepted, reason=reason,
                        note='Execution and type checks do not prove semantic correctness')
            if not accepted:
                return {'sql': previous['sql'], 'feedback': previous['feedback'],
                        'output_type_report': previous['output_type_report'],
                        'repair_previous': {}, 'repair_rejected': True}
            return {'feedback': feedback, 'output_type_report': type_report,
                    'repair_previous': {}, 'repair_rejected': False}
        return {"feedback": feedback, 'output_type_report': type_report}

    def _check_output(self, state, sql, feedback, *, phase='execution'):
        from .output_types import check_output_types
        report = (check_output_types(sql, feedback, state.get('full_schema', {}), self.config.output_types)
                  if self.config.output_type_check else {'status': 'disabled'})
        self._event(state, 'output_type_checked', sql=sql, phase=phase, report=report)
        return report

    def run(self, input_data: Question):
        if not isinstance(input_data, Question):
            raise TypeError("run() accepts Question, which deliberately has no gold SQL field")
        state = {"question": input_data, "run_id": uuid4().hex, "repair_count": 0,
                 "schema_refresh_count": 0, "unchanged_sql": False,
                 "semantic_status": "disabled"}
        self._event(state, "workflow_started",
                    workflow_mode='optional_semantic_review' if self.config.semantic_review else 'execution_repair_only',
                    max_repairs=self.config.max_repairs, semantic_review=self.config.semantic_review)
        try:
            result = self.graph.invoke(state, {"recursion_limit": 20 + 2 * self.config.max_repairs})
        except Exception as error:
            self._event(state, "workflow_failed", error_type=type(error).__name__,
                        code=getattr(error, 'code', type(error).__name__), error=str(error))
            raise
        if result.get('semantic_status') == 'pending':
            type_bad = result.get('output_type_report', {}).get('status') == 'mismatch'
            result['semantic_status'] = 'skipped_type_mismatch' if type_bad else 'skipped_execution_error'
            self._event(state, 'semantic_review_skipped',
                        reason='Unresolved output type mismatch' if type_bad else 'SQL did not execute successfully')
        self._event(state, "workflow_completed", sql=result["sql"],
                    status=result["feedback"]["status"], repair_count=result["repair_count"],
                    schema_refresh_count=result["schema_refresh_count"],
                    repair_rejected=result.get('repair_rejected', False),
                    semantic_status=result.get('semantic_status', 'disabled'),
                    semantic_review_count=result.get('semantic_review_count', 0),
                    semantic_revision_count=result.get('semantic_revision_count', 0),
                    semantic_metrics=result.get('semantic_metrics', {}),
                    semantic_validation=result.get('semantic_validation', {}),
                    output_columns_report=result.get('output_columns_report', {}),
                    output_requirements=result.get('output_requirements', {}),
                    output_requirements_digest=result.get('output_requirements_digest'),
                    output_requirements_count=result.get('output_requirements_count', 0),
                    output_requirements_status=result.get('output_requirements_status', 'not_run'),
                    output_requirements_assessment=result.get('output_requirements_assessment', {}),
                    output_requirements_protocol_warnings=result.get('output_requirements_protocol_warnings', []),
                    output_projection_suggestion=result.get('output_projection_suggestion', {}),
                    semantic_suggestion=result.get('semantic_suggestion', {}),
                    output_type_check=result.get('output_type_report', {'status': 'unverified'}))
        return result["sql"]
