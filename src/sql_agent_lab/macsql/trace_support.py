"""Shared run identity and timing for workflow, tool and evaluator events."""
from datetime import datetime, timezone
import time


def failure_category(code='', message=''):
    value = str(code).casefold()
    if value in ('output_truncated', 'length'):
        return 'model_output_truncated'
    if value in ('contextbudgetexceeded', 'budget_exhausted'):
        return 'budget_limit'
    if value in ('missing_column', 'missing_table', 'ambiguous_column'):
        return 'sql_identifier_error'
    if 'timeout' in value or str(message).casefold() == 'interrupted':
        return 'execution_timeout' if value in ('operationalerror', '') else 'request_timeout'
    if value in ('readerror', 'connecterror', 'http_error', 'unknown_usage', 'missing_usage'):
        return 'model_request_error'
    if value in ('operationalerror', 'programmingerror', 'databaseerror', 'integrityerror'):
        return 'sql_execution_error'
    if value in ('roleoutputerror',):
        return 'model_output_protocol_error'
    return 'unclassified_error'


def event_category(event):
    name = event.get('event')
    if name == 'output_type_checked':
        return {'mismatch': 'output_type_error', 'unverified': 'output_type_warning',
                'skipped': 'type_check_not_run'}.get(event.get('report', {}).get('status'))
    if name == 'semantic_review_skipped' or (name == 'workflow_completed' and event.get('semantic_status') == 'disabled'):
        return 'semantic_not_run'
    if name in ('execution', 'sql_validation'):
        f = event.get('feedback', {})
        if f.get('status') == 'error':
            return failure_category(f.get('identifier_kind', f.get('error_type', '')), f.get('message', ''))
    if name in ('call_failed', 'workflow_failed', 'tool_failed', 'output_requirements_error', 'semantic_review_error'):
        category = failure_category(event.get('code', event.get('error_type', '')), event.get('error', ''))
        if category == 'unclassified_error' and name in ('output_requirements_error', 'semantic_review_error'):
            return 'review_report_error'
        return category
    return None


def review_timing(result, events=()):
    """Read legacy records without treating extraction time as SQL review time."""
    metrics = result.get('semantic_metrics') or {}
    extraction_failed = result.get('semantic_status') == 'requirements_unverified'
    if not extraction_failed:
        extraction_failed = any(e.get('event') == 'output_requirements_error' for e in events) and not any(
            e.get('event') == 'semantic_review_started' for e in events)
    reviewed_states = {'accepted', 'accepted_with_warnings', 'uncertain', 'output_unverified',
                       'output_mismatch', 'projection_suggested', 'semantic_suggested',
                       'revision_guard_rejected', 'revised_unverified', 'revision_rejected'}
    started = any(e.get('event') == 'semantic_review_started' for e in events) or result.get('semantic_status') in reviewed_states
    frozen = next((e for e in events if e.get('event') == 'output_requirements_frozen'), {})
    requirements = metrics.get('requirements_seconds', frozen.get('elapsed_seconds'))
    sql_seconds = metrics.get('sql_review_seconds')
    if extraction_failed:
        requirements = requirements if requirements is not None else metrics.get('elapsed_seconds')
        sql_seconds = None
    elif sql_seconds is None and (started or metrics.get('duration_scope') == 'sql_review_only_excludes_requirements'):
        sql_seconds = metrics.get('elapsed_seconds')
    return {'requirements_seconds': requirements, 'sql_review_seconds': sql_seconds,
            'sql_review_ran': False if extraction_failed or result.get('semantic_status') == 'disabled' else
                (True if started or sql_seconds is not None else None),
            'total_review_seconds': result.get('added_review_seconds', metrics.get('total_review_seconds',
                requirements if extraction_failed else None)),
            'run_seconds': result.get('elapsed_seconds')}


class TraceEmitter:
    def __init__(self, writer, run_id, question_id, *, started=None):
        self.writer, self.run_id, self.question_id = writer, run_id, str(question_id)
        self.started = time.monotonic() if started is None else started
        self.sequence = 0

    def __call__(self, event):
        self.sequence += 1
        category = event_category(event)
        self.writer({**event, **({'diagnostic_category': category} if category else {}),
            'trace_schema_version':3,'run_id':self.run_id,
            'question_id':self.question_id,'event_sequence':self.sequence,
            'timestamp':datetime.now(timezone.utc).isoformat(),
            'elapsed':time.monotonic()-self.started})
