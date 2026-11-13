"""Reference-style MAC-SQL three-role workflow for SQLite benchmarks."""

from .reference_baseline import ReferenceMacSQLAgent, make_agent_class
from .reference_core import WorkflowConfig, ModelReply, ROLES, JsonlTrace
from .dataset import Question, Reference, load_bird_jsonl, score_sql

__all__ = ["ReferenceMacSQLAgent", "make_agent_class", "WorkflowConfig", "ModelReply", "ROLES",
           "JsonlTrace", "Question", "Reference", "load_bird_jsonl", "score_sql"]
