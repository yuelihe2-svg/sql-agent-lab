"""Application task state, independent of SQL checks and answer scores.

The web service owns one worker at a time. Reading an abandoned task is a
read-only recovery view: it never resumes work or rewrites historical records.
"""
from datetime import datetime, timezone
import json


LIVE_STATES = {'running', 'stopping'}
TERMINAL_STATES = {'completed', 'failed', 'stopped'}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path, value):
    """Readers see either the previous JSON or the complete replacement."""
    temporary = path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def new_task():
    now = utc_now()
    return {'state': 'running', 'stage': 'preparing', 'started_at': now,
            'updated_at': now, 'ended_at': None, 'stop_requested_at': None,
            'error': None, 'source': 'task'}


def change_task(task, *, state=None, stage=None, error=None):
    """Validate transitions; callers serialize read/change/write with a lock."""
    target = state or task['state']
    allowed = {'running': {'running', 'stopping', *TERMINAL_STATES},
               'stopping': {'stopping', *TERMINAL_STATES}}
    if target not in allowed.get(task['state'], {task['state']}):
        raise ValueError(f"Invalid task transition: {task['state']} -> {target}")
    if task['state'] in TERMINAL_STATES:
        return dict(task)
    now = utc_now()
    updated = {**task, 'state': target, 'updated_at': now}
    if stage is not None:
        updated['stage'] = stage
    if target == 'stopping' and not task['stop_requested_at']:
        updated['stop_requested_at'] = now
    if target in TERMINAL_STATES:
        updated.update(ended_at=now, stage='finished', error=error)
    return updated


def result_task_state(result):
    # A wrong answer or unresolved type check is still a finished task.
    return {'completed': 'completed', 'needs_review': 'completed',
            'failed': 'failed', 'evaluation_failed': 'failed',
            'stopped': 'stopped'}.get((result or {}).get('status'), 'unknown')


def task_view(path, result, *, active=False, stop_requested=False, managed=False, current=None):
    """Project saved state for the API without writing anything on GET.

    Only new, locally managed tasks can be classified as interrupted. Missing
    state in old research records remains unknown; scores never imply success.
    """
    saved = path / 'task.json'
    task = dict(current) if current is not None else (
        json.loads(saved.read_text(encoding='utf8')) if saved.exists() else None)
    if task is None:
        return {'state': ('stopping' if stop_requested else 'running') if active else result_task_state(result),
                'stage': None, 'started_at': None, 'updated_at': None,
                'ended_at': None, 'stop_requested_at': None,
                'error': (result or {}).get('error'), 'source': 'legacy'}
    if not active and task['state'] in LIVE_STATES:
        final = result_task_state(result)
        if final != 'unknown':
            # A crash can happen after result.json is saved but before task.json.
            task.update(state=final, stage='finished', error=result.get('error'), source='result_recovery')
        elif managed:
            task.update(state='interrupted', error='The service stopped before the task saved a final result. It was not rerun automatically.',
                        source='interruption_recovery')
    return task


def event_stage(event):
    """Translate existing observations into UI stages; never route the Agent."""
    if event['event'] == 'call_started':
        return {'output_requirements': 'requirements', 'semantic_review': 'semantic_review'}.get(
            event.get('purpose'), event.get('role'))
    if event['event'] == 'tool_started':
        return {'load_schema': 'schema', 'search_schema': 'schema', 'lookup_values': 'schema',
                'compile_sql_readonly': 'execute', 'execute_sql_readonly': 'execute'}.get(event.get('tool'))
    return {'run_preparing': 'preparing', 'model_ready': 'schema',
            'evaluation_started': 'evaluation', 'workflow_completed': 'finalizing'}.get(event['event'])
