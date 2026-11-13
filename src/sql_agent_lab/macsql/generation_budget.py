"""Input-only complexity hints; no reference answers or model calls."""
import re


def generation_budget(question, *, base=1000, complex_limit=2400, sql=''):
    text = question.question + '\n' + (question.evidence or '')
    words = len(text.split())
    signals = [name for name, pattern in {
        'aggregation': r'\b(average|mean|total|count|percentage|ratio|how many)\b',
        'grouping': r'\b(each|per|every|group)\b',
        'ranking': r'\b(highest|lowest|most|least|rank|ties|earliest|latest)\b',
        'time': r'\b(as of|before|after|during|season)\b|\b(?:19|20)\d{2}\b',
    }.items() if re.search(pattern, text, re.I)]
    complex_query = words >= 40 or len(text) >= 400 or len(signals) >= 2 or len(sql.encode('utf8')) >= 1500
    return {'max_output_tokens': max(base, complex_limit) if complex_query else base,
            'budget_policy': 'input_complexity_v1', 'complex_query': complex_query,
            'complexity_signals': signals, 'question_word_count': words,
            'automatic_retry': False}
