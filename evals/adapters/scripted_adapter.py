"""Replay prepared answers without rewriting history into the current question."""

from __future__ import annotations


class ScriptedConversationAdapter:
    name = "scripted"

    def __init__(self, scripts: dict):
        self.scripts = scripts
        self.seen = []

    def run_turn(self, *, conversation_id, turn_id, question, history, source_id):
        self.seen.append({
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "source_id": source_id,
            "question": question,
            "history_questions": [item["question"] for item in history],
        })
        script = self.scripts[(conversation_id, turn_id)]
        return {
            "predicted_sql": script.get("predicted_sql"),
            "answer": script.get("answer"),
            "tool_calls": script.get("tool_calls"),
            "tool_errors": script.get("tool_errors"),
            "schema_retrieval_calls": script.get("schema_retrieval_calls"),
            "sql_execution_calls": script.get("sql_execution_calls"),
            "repair_calls": script.get("repair_calls"),
            "model_calls_by_role": script.get("model_calls_by_role"),
            "input_tokens": script.get("input_tokens"),
            "output_tokens": script.get("output_tokens"),
            "elapsed_ms": script.get("elapsed_ms"),
            "estimated_cost": script.get("estimated_cost"),
            "is_agent_result": False,
        }
