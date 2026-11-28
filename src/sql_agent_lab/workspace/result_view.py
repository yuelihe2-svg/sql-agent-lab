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
    'tokens', 'evaluated', 'grounding', 'needs_clarification', 'citations',
)


def public_result(directory, *, state, stage, result, task_error=None):
    """Build the chat DTO from an already loaded result object."""
    loaded = result if isinstance(result, dict) else {}
    feedback = loaded.get('feedback') if isinstance(loaded.get('feedback'), dict) else {}
    columns, column_cut = _columns(feedback.get('columns'))
    rows, row_cut = _rows(feedback.get('preview_rows'), len(columns))
    has_more, truncated = _bounds(feedback, row_cut or column_cut)
    if feedback.get('column_count', len(columns)) != len(columns):
        truncated = True
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
    from .answer_checks import result_caveat, query_basis
    summary = _summary(state, loaded, feedback, columns, rows, complete, error)
    caveat = result_caveat({**feedback, 'has_more': has_more, 'truncated': truncated}) if executed and summary else ''
    if caveat:
        summary += ' ' + caveat
    if summary and executed and sql:
        summary += '\n' + query_basis(sql)
    return {
        'summary': summary,
        'citations': loaded.get('citations', []),
        'needs_clarification': isinstance(loaded.get('clarification'), str) and bool(loaded['clarification']),
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
        'tokens': _tokens(directory, loaded),
        'evaluated': _evaluated(loaded),
        'grounding': _grounding(loaded),
    }


def public_notice(value):
    """Drop path-bearing text before it reaches the chat API."""
    if not isinstance(value, str) or not value.strip():
        return None
    if _PATH.search(value):
        return '查询未能完成。'
    return value.strip()[:500]


def _bounds(feedback, row_cut):
    if not feedback:
        return False, False
    has_more = feedback.get('has_more') is not False
    truncated = feedback.get('truncated') is not False
    if row_cut:
        truncated = True
    return has_more, truncated


def _columns(value):
    if not isinstance(value, list) or not value:
        return [], True
    names = []
    cut = len(value) > _COLUMNS
    for index, item in enumerate(value[:_COLUMNS], start=1):
        name = item if isinstance(item, str) and item.strip() else f'第{index}列'
        names.append(name[:_CELL])
        cut |= len(name) > _CELL
    return names, cut


def _rows(value, width):
    if not isinstance(value, list):
        return [], True
    cut = len(value) > _ROWS
    rows = []
    for raw in value[:_ROWS]:
        if not isinstance(raw, list):
            cut = True
            continue
        if len(raw) != width or any(isinstance(cell, str) and len(cell) > _CELL for cell in raw[:width]):
            cut = True
        rows.append([_cell(item) for item in raw[:width]])
    return rows, cut


def _cell(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        # Browser JSON parsers round integers outside the IEEE-754 safe range.
        return value if abs(value) <= 2**53 - 1 else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
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
        return 'SQL 已执行，但任务未正常完成。下方保留已取得的结果，请在详细视图检查失败原因。'
    raw = task_error or result.get('error') or feedback.get('message') or feedback.get('error') or ''
    return _friendly(raw)


def _friendly(message):
    raw = public_notice(message) or ''
    if re.search(r'no such table|no such column', raw, re.I):
        return '未找到对应的表或字段，请核对名称，或补充问题中的业务含义。'
    if re.search(r'syntax error|incomplete input|unrecognized token', raw, re.I):
        return '生成的 SQL 存在语法问题，未能执行成功。请调整问题描述后重试，并展开 SQL 核对。'
    if re.search(r'not authorized|readonly|read.only|only.*select', raw, re.I):
        return '这条 SQL 未通过只读检查。请改为查询问题，本应用不执行数据修改。'
    if re.search(r'unable to open database|file is not a database', raw, re.I):
        return '无法打开数据源，请检查 SQLite 文件路径和访问权限。'
    if re.search(r'database is locked', raw, re.I):
        return '数据库当前被占用，请稍后重试。'
    if re.search(r'timeout|timed out|interrupted', raw, re.I):
        return '本次处理超时，可以缩小查询范围后重试。'
    if re.search(r'connect|connection|ollama', raw, re.I):
        return '未能连接本地模型服务，请检查服务后重试。'
    if re.search(r'already active', raw, re.I):
        return '当前已有查询在运行，请等待结束。'
    if not raw:
        return '查询未能完成。可切换到详细视图查看原因，调整问题后重试。'
    return raw


def _summary(state, result, feedback, columns, rows, complete, error):
    if result.get('answer'):
        return result['answer']
    if result.get('clarification'):
        return public_notice(result['clarification']) or '请补充查询条件。'
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
        text = '查询已成功执行，没有匹配的记录。可以检查筛选条件，或适当扩大查询范围。'
    elif (complete and len(rows) == 1 and len(columns) == 1 and column_count == 1
          and len(rows[0]) == 1):
        value = rows[0][0]
        label = '「' + columns[0] + '」'
        if value is None:
            text = label + '的查询结果为 NULL（空值）。这不等于 0，也不代表没有返回记录。'
        elif value == '':
            text = label + '的查询结果为空字符串（长度为 0 的文本），不等于 NULL。'
        else:
            shown = _format_value(value)
            text = label + '的查询结果是 ' + shown + '。'
    elif complete:
        text = ('查询返回了 ' + str(len(rows)) + ' 行、' + str(len(columns))
                + ' 列结果，详见下方表格。')
    else:
        text = ('当前预览显示 ' + str(len(rows)) + ' 行、' + str(len(columns))
                + ' 列。结果或字段值可能未完整展示，不能把预览行数当作总数。')
    if result.get('status') == 'needs_review':
        text += ' 返回类型检查仍有待核对，请结合 SQL 和详细记录确认。'
    return text


def _format_value(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def _tokens(directory, result):
    from sql_agent_lab.macsql.trace_support import token_totals
    empty = {'input': None, 'output': None}
    saved = result.get('tokens')
    if isinstance(saved, dict):
        if all(type(saved.get(k)) is int and saved[k] >= 0 for k in empty):
            return {k: saved[k] for k in empty}
        return empty
    if directory is None:
        return empty
    path = Path(directory) / 'trace.jsonl'
    if not path.is_file():
        return empty
    try:
        with path.open(encoding='utf8') as stream:
            return token_totals(json.loads(line) for line in stream)
    except (OSError, ValueError):
        return empty
