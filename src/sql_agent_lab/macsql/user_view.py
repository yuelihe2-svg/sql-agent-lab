"""Small, display-only views for the single-question page."""
from collections import deque
import json

from sql_agent_lab.workspace.result_view import public_notice, public_result
from .trace_support import token_totals


def public_question(manifest):
    question = manifest.get('question') or {}
    return {k: question.get(k) for k in ('question_id', 'db_id', 'question')}


def user_task(task):
    view = {k: task.get(k) for k in (
        'state', 'stage', 'started_at', 'updated_at', 'ended_at', 'stop_requested_at')}
    for key in ('error', 'persistence_error'):
        if task.get(key):
            view[key] = public_notice(task[key])
    return view


def user_run(path, run_id, manifest, result, task, *, active, stop_requested):
    recent = deque(maxlen=100)
    calls = []
    count = started = 0
    simulated = False
    trace = path / 'trace.jsonl'
    if trace.is_file():
        with trace.open(encoding='utf8') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                count += 1
                kind = event.get('event')
                started += kind == 'call_started'
                simulated |= event.get('simulated') is True
                if kind in ('call_started', 'call_completed', 'call_failed'):
                    calls.append({k: event.get(k) for k in ('event', 'usage_complete', 'input_tokens', 'output_tokens')})
                row = {k: event[k] for k in ('event', 'role', 'tool', 'simulated') if k in event}
                row['elapsed'] = event.get('elapsed', event.get('elapsed_seconds_since_start'))
                recent.append(row)
    visible = None
    if isinstance(result, dict):
        view = public_result(None, state=task['state'], stage=task.get('stage'), result=result)
        visible = {k: result.get(k) for k in ('status', 'score', 'elapsed_seconds')}
        visible.update(sql=view['sql'], error=public_notice(result.get('error')))
        if isinstance(result.get('answer') or result.get('clarification'), str):
            visible['answer'] = view['summary']
        if isinstance(result.get('feedback'), dict):
            raw = result['feedback']
            visible['feedback'] = {
                'status': raw.get('status'), 'columns': view['columns'], 'preview_rows': view['rows'],
                'column_count': raw.get('column_count'), 'has_more': view['has_more'],
                'truncated': view['truncated'], 'message': public_notice(raw.get('message') or raw.get('error'))}
    return {'id': run_id, 'active': active, 'stop_requested': stop_requested,
            'manifest': {'question': public_question(manifest), 'benchmark': manifest.get('benchmark') is True},
            'task': user_task(task), 'result': visible, 'events': list(recent), 'event_count': count,
            'usage': {'calls': started, 'tokens': token_totals(calls), 'simulated': simulated}}
