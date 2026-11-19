"""User-facing result view. Reads a saved run and returns a fixed set of fields.

The run directory stays the source of truth. This view never copies trace
events, prompts, reference SQL, schema samples, or filesystem paths.
"""
from pathlib import Path
import json
import math
import re


_PATH = re.compile(r'(?:[A-Za-z]:[\\/][^\s\'"]+|/(?:Users|home|tmp|var|opt)[^\s\'"]+)')
_STAGE = re.compile(r'[a-z_]{1,40}')
_COLUMNS = 12
_ROWS = 5
_CELL = 160
_SQL = 20000

VIEW_FIELDS = (
    'summary', 'state', 'stage', 'execution_status', 'sql', 'columns', 'rows',
    'preview', 'has_more', 'truncated', 'complete', 'error', 'elapsed_seconds',
    'tokens', 'evaluated', 'grounding',
)


def public_result(directory, *, state, stage, result, task_error=None):
    """Build the chat DTO from an already loaded result object."""
    loaded = result if isinstance(result, dict) else {}
    feedback = loaded.get('feedback') if isinstance(loaded.get('feedback'), dict) else {}
    columns = _columns(feedback.get('columns'))
    rows, row_cut = _rows(feedback.get('preview_rows'), len(columns) or _COLUMNS)
    has_more, truncated = _bounds(feedback, row_cut)
    executed = feedback.get('status') == 'ok'
    sql = loaded.get('sql') if isinstance(loaded.get('sql'), str) else None
    if isinstance(sql, str):
        sql = sql.strip() or None
        if sql is not None and len(sql) > _SQL:
            sql = sql[:_SQL]
            truncated = True
    complete = bool(executed and feedback and has_more is False and truncated is False and sql)
    elapsed = _elapsed(loaded.get('elapsed_seconds'))
    error = _error_summary(state, loaded, feedback, task_error)
    return {
        'summary': _summary(state, loaded, feedback, columns, rows, complete, error),
        'state': state if isinstance(state, str) else 'unknown',
        'stage': stage if isinstance(stage, str) and _STAGE.fullmatch(stage) else None,
        'execution_status': loaded.get('status') if isinstance(loaded.get('status'), str) else None,
        'sql': sql,
        'columns': columns,
        'rows': rows,
        'preview': bool(feedback),
        'has_more': has_more,
        'truncated': truncated,
        'complete': complete,
        'error': error,
        'elapsed_seconds': elapsed,
        'tokens': _tokens(directory),
        'evaluated': _evaluated(loaded),
        'grounding': _grounding(loaded),
    }


def public_notice(value):
    """Drop path-bearing text before it reaches the chat API."""
    if not isinstance(value, str) or not value.strip():
        return None
    if _PATH.search(value):
        return 'The query did not finish.'
    return value.strip()[:500]


def _bounds(feedback, row_cut):
    if not feedback:
        return False, False
    has_more = True if 'has_more' not in feedback else feedback.get('has_more') is True
    truncated = True if 'truncated' not in feedback else feedback.get('truncated') is True
    if row_cut:
        truncated = True
    return has_more, truncated


def _columns(value):
    if not isinstance(value, list):
        return []
    names = []
    for item in value[:_COLUMNS]:
        if isinstance(item, str) and item.strip():
            names.append(item.strip()[:_CELL])
    return names


def _rows(value, width):
    if not isinstance(value, list):
        return [], False
    cut = len(value) > _ROWS
    rows = []
    for raw in value[:_ROWS]:
        if not isinstance(raw, list):
            continue
        if len(raw) > width:
            cut = True
        rows.append([_cell(item) for item in raw[:width]])
    return rows, cut


def _cell(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value if len(value) <= _CELL else value[:_CELL] + '…'
    return None


def _elapsed(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 3)


def _evaluated(result):
    """Execution success is not a score. Only a recorded 0/1 evaluation counts."""
    score = result.get('score')
    if isinstance(score, bool) or score not in (0, 1, 0.0, 1.0):
        return False
    return isinstance(result.get('evaluation_status'), str) and bool(result.get('evaluation_status'))


def _grounding(result):
    """Expose whether grounding was used. Absence means it was not used."""
    raw = result.get('grounding')
    if not isinstance(raw, dict) or raw.get('enabled') is not True:
        return {'enabled': False, 'proves_sql': False, 'estimated_api_cost': None}
    knowledge = []
    for item in raw.get('knowledge') or []:
        if not isinstance(item, dict):
            continue
        knowledge.append({
            'id': item.get('id') if isinstance(item.get('id'), str) else None,
            'version': item.get('version') if isinstance(item.get('version'), int) else None,
            'content_hash': item.get('content_hash') if isinstance(item.get('content_hash'), str) else None,
            'title': item.get('title').strip()[:200] if isinstance(item.get('title'), str) else '',
        })
    return {
        'enabled': True,
        'datasource_id': raw.get('datasource_id') if isinstance(raw.get('datasource_id'), str) else None,
        'fingerprint': raw.get('fingerprint') if isinstance(raw.get('fingerprint'), str) else None,
        'matched_tables': [name for name in raw.get('matched_tables') or [] if isinstance(name, str)][:8],
        'matched_columns': [name for name in raw.get('matched_columns') or [] if isinstance(name, str)][:16],
        'knowledge': knowledge[:5],
        'knowledge_skipped': [item.get('id') for item in raw.get('knowledge_skipped') or []
                              if isinstance(item, dict) and isinstance(item.get('id'), str)][:5],
        'context_chars': raw.get('context_chars') if isinstance(raw.get('context_chars'), int) else None,
        'retrieval_latency_ms': _latency(raw.get('retrieval_latency_ms')),
        'agent_latency_ms': _latency(raw.get('agent_latency_ms')),
        'retrieval_cache': raw.get('retrieval_cache') if raw.get('retrieval_cache') in ('hit', 'miss') else None,
        'strategy': raw.get('strategy') if raw.get('strategy') in ('keyword', 'bm25', 'hybrid') else 'keyword',
        'proves_sql': False,
        'estimated_api_cost': None,
    }


def _latency(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return round(float(value), 1)


def _error_summary(state, result, feedback, task_error):
    failed = state == 'failed' or feedback.get('status') == 'error' or result.get('status') in (
        'failed', 'evaluation_failed')
    if not failed:
        return None
    if feedback.get('status') == 'ok':
        return 'The SQL ran, but the task did not finish normally. The rows already returned are kept below. Check the detail view for the cause.'
    raw = task_error or result.get('error') or feedback.get('message') or feedback.get('error') or ''
    return _friendly(raw)


def _friendly(message):
    raw = public_notice(message) or ''
    if re.search(r'no such table|no such column', raw, re.I):
        return 'No matching table or column was found. Check the names, or add the business meaning to the question.'
    if re.search(r'syntax error|incomplete input|unrecognized token', raw, re.I):
        return 'The generated SQL has a syntax problem and did not run. Adjust the question and check the SQL.'
    if re.search(r'not authorized|readonly|read.only|only.*select', raw, re.I):
        return 'This SQL failed the read-only check. Ask a question. This application does not modify data.'
    if re.search(r'unable to open database|file is not a database', raw, re.I):
        return 'The data source could not be opened. Check the SQLite file path and permissions.'
    if re.search(r'database is locked', raw, re.I):
        return 'The database is busy. Try again shortly.'
    if re.search(r'timeout|timed out|interrupted', raw, re.I):
        return 'This request timed out. Narrow the query and try again.'
    if re.search(r'connect|connection|ollama', raw, re.I):
        return 'The local model service could not be reached. Check the service and try again.'
    if re.search(r'already active', raw, re.I):
        return 'A query is already running. Wait for it to finish.'
    if not raw:
        return 'The query did not finish. Open the detail view, adjust the question, and try again.'
    return raw


def _summary(state, result, feedback, columns, rows, complete, error):
    if state in ('running', 'stopping', 'stopped', 'interrupted'):
        return None
    if state == 'failed' or feedback.get('status') == 'error':
        return error
    if feedback.get('status') != 'ok':
        return None
    if not isinstance(rows, list) or not isinstance(columns, list):
        return None
    column_count = feedback.get('column_count')
    if complete and len(rows) == 0:
        text = 'The query ran and returned no matching rows. Check the filters, or widen the question.'
    elif (complete and len(rows) == 1 and len(columns) == 1 and column_count == 1
          and len(rows[0]) == 1):
        value = rows[0][0]
        label = '"' + columns[0] + '"'
        if value is None:
            text = label + ' is NULL. That is not 0, and it does not mean no row was returned.'
        elif value == '':
            text = label + ' is an empty string. That is not NULL.'
        else:
            shown = _format_value(value)
            text = label + ' is ' + shown + '.'
    elif complete:
        text = ('The query returned ' + str(len(rows)) + ' rows and ' + str(len(columns))
                + ' columns. See the table below.')
    else:
        text = ('The preview shows ' + str(len(rows)) + ' rows and ' + str(len(columns))
                + ' columns. Rows or values may be incomplete. Do not treat the preview count as the total.')
    if result.get('status') == 'needs_review':
        text += ' The output-type check still needs review. Confirm it against the SQL and the detail record.'
    return text


def _format_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return format(value, 'g')
    return str(value)


def _tokens(directory):
    empty = {'input': None, 'output': None}
    if directory is None:
        return empty
    path = Path(directory) / 'trace.jsonl'
    if not path.is_file():
        return empty
    total_in = total_out = 0
    seen = False
    try:
        lines = path.read_text(encoding='utf8').splitlines()
    except OSError:
        return empty
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return empty
        if not isinstance(event, dict) or event.get('event') not in ('call_completed', 'call_failed'):
            continue
        if event.get('usage_complete') is False:
            return empty
        incoming, outgoing = event.get('input_tokens'), event.get('output_tokens')
        if isinstance(incoming, bool) or isinstance(outgoing, bool):
            return empty
        if not isinstance(incoming, int) or not isinstance(outgoing, int):
            return empty
        seen = True
        total_in += incoming
        total_out += outgoing
    if not seen:
        return empty
    return {'input': total_in, 'output': total_out}
