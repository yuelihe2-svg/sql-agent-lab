"""Turn accuracy and session success use separate denominators."""

from __future__ import annotations


def _turn_outcome(turn: dict) -> str:
    if turn.get("gold_status") == "not_implemented":
        return "not_implemented"
    if turn.get("gold_status") == "skipped":
        return "skipped"
    if turn.get("sql_status") in {"evaluation_error", "timeout"}:
        return turn["sql_status"]
    if turn.get("answer_status") in {"evaluation_error", "timeout"}:
        return turn["answer_status"]
    return "scored"


def turn_is_eligible(turn: dict) -> bool:
    if _turn_outcome(turn) != "scored":
        return False
    if turn.get("sql_status") != "scored":
        return False
    answer_status = turn.get("answer_status", "not_required")
    return answer_status in {"scored", "not_required"}


def turn_is_correct(turn: dict) -> bool:
    if not turn_is_eligible(turn):
        return False
    if turn.get("sql_correct") is not True:
        return False
    if turn.get("answer_status") == "scored":
        return turn.get("answer_correct") is True
    return True


def summarize_turns(turns: list[dict]) -> dict:
    counts = {"not_implemented": 0, "evaluation_error": 0, "timeout": 0, "skipped": 0, "manual_review": 0}
    eligible = [turn for turn in turns if turn_is_eligible(turn)]
    correct = [turn for turn in eligible if turn_is_correct(turn)]
    for turn in turns:
        outcome = _turn_outcome(turn)
        if outcome in counts:
            counts[outcome] += 1
        if turn.get("answer_status") == "manual_review":
            counts["manual_review"] += 1
    sql_scored = [turn for turn in turns if turn.get("sql_status") == "scored"]
    answer_scored = [turn for turn in turns if turn.get("answer_status") == "scored"]
    return {
        "eligible_turns": len(eligible),
        "correct_turns": len(correct),
        "turn_accuracy": None if not eligible else len(correct) / len(eligible),
        "sql_eligible": len(sql_scored),
        "sql_correct": sum(1 for turn in sql_scored if turn.get("sql_correct") is True),
        "sql_accuracy": None if not sql_scored else sum(1 for turn in sql_scored if turn.get("sql_correct") is True) / len(sql_scored),
        "answer_eligible": len(answer_scored),
        "answer_correct": sum(1 for turn in answer_scored if turn.get("answer_correct") is True),
        "answer_accuracy": None if not answer_scored else sum(1 for turn in answer_scored if turn.get("answer_correct") is True) / len(answer_scored),
        **counts,
    }


def summarize_sessions(sessions: list[dict]) -> dict:
    """A session with an unsupported turn is not in the success denominator."""
    eligible = []
    successful = []
    unsupported = []
    errored = []
    for session in sessions:
        turns = session["turns"]
        if any(turn.get("gold_status") == "not_implemented" for turn in turns):
            unsupported.append(session["conversation_id"])
            continue
        if any(turn.get("sql_status") in {"evaluation_error", "timeout"} for turn in turns):
            errored.append(session["conversation_id"])
            continue
        if any(not turn_is_eligible(turn) for turn in turns):
            unsupported.append(session["conversation_id"])
            continue
        eligible.append(session["conversation_id"])
        if all(turn_is_correct(turn) for turn in turns):
            successful.append(session["conversation_id"])
    turn_summary = summarize_turns([turn for session in sessions for turn in session["turns"]])
    return {
        "eligible_sessions": len(eligible),
        "fully_correct_sessions": len(successful),
        "session_success": None if not eligible else len(successful) / len(eligible),
        "unsupported_sessions": unsupported,
        "evaluation_error_sessions": errored,
        **turn_summary,
    }
