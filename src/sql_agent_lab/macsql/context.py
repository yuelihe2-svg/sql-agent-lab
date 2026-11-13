"""Conservative context accounting for reference-style MAC-SQL prompts."""

from dataclasses import dataclass
from typing import Callable


class ContextBudgetExceeded(ValueError):
    """Required input cannot fit without dropping question or schema identifiers."""


def byte_token_bound(messages: list[dict]) -> int:
    """Conservative UTF-8 byte bound; this is not billed model usage."""
    return 64 + sum(16 + len(message["content"].encode("utf-8")) for message in messages)


@dataclass(frozen=True)
class ContextPolicy:
    input_budget: int = 24_000
    output_reserve: int = 1_000
    context_window: int = 32_000
    safety_margin: int = 512
    count_tokens: Callable = byte_token_bound

    def __post_init__(self):
        if min(self.input_budget, self.output_reserve, self.context_window) <= 0:
            raise ValueError("Context budgets must be positive")
        if self.safety_margin < 0 or (
            self.input_budget + self.output_reserve + self.safety_margin > self.context_window
        ):
            raise ValueError("Input, output and safety budgets exceed the context window")
