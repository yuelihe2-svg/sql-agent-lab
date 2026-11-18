"""Interfaces for future agent variants. None of them call a model in this build."""

from __future__ import annotations


VARIANTS = ("single", "three_role", "three_role_rag", "three_role_memory")


class AgentVariantAdapter:
    def __init__(self, name: str):
        if name not in VARIANTS:
            raise ValueError(f"Unknown agent variant: {name}")
        self.name = name

    def run_turn(self, **_kwargs):
        return {
            "status": "not_implemented",
            "variant": self.name,
            "is_agent_result": False,
            "reason": "conversation_memory" if self.name == "three_role_memory" else "agent_variant_not_wired",
        }


def refuse_agent_mode(*, model: str | None, max_cost_usd: float | None, authorize: bool) -> str:
    if not model or max_cost_usd is None or not authorize:
        raise SystemExit("Agent mode refused: model, max-cost-usd, and explicit authorization are required")
    raise SystemExit("Agent mode refused: no model client is wired, so this build will not call a model")
