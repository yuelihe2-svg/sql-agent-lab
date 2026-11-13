"""Post-model evaluation evidence and per-session reuse. Never passed to an agent."""
from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
import math
import re
import sqlite3
import time

from .tools import EvaluationLimitExceeded

SCORER = 'complete_row_set_v1_ignore_order_and_duplicates'


def digest(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def canonical(value):
    if value is None: return ['null']
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value): return ['number', str(value)]
        rational = Fraction(value)
        return ['number', rational.numerator, rational.denominator]
    if isinstance(value, bytes): return ['blob', value.hex()]
    return ['text', value]


def row_key(row):
    return json.dumps([canonical(v) for v in row], ensure_ascii=False, separators=(',', ':'))


def summarize(result):
    rows = sorted(row_key(row) for row in result['rows'])
    return {k:result[k] for k in ('columns','row_count','unique_row_count','column_count')} | {
        'result_sha256':digest('\n'.join(rows)), 'complete':True}


def examples(rows):
    # Keep small diagnostic examples in the evaluation artifact only.
    result = []
    for row in sorted(rows, key=row_key)[:3]:
        result.append([{'value':repr(v)[:160], 'truncated':len(repr(v))>160,
                        'storage_type':type(v).__name__} for v in row[:12]])
    return {'rows':result,'truncated':len(rows)>3 or any(len(r)>12 for r in rows)}


class EvaluationSession:
    """One read-only database/reference/configuration; no persistent shared cache."""
    def __init__(self, tools, reference, database_sha256, *, artifact=None, emit=None):
        self.tools, self.reference = tools, reference
        self.artifact, self.emit = artifact, emit or (lambda e:None)
        self.cache, self.comparisons = {}, []
        self.path = tools.databases[reference.db_id]
        self.stamp = (self.path.stat().st_size, self.path.stat().st_mtime_ns)
        self.identity = {'db_id':reference.db_id,'database_sha256':database_sha256,
            'reference_sql_sha256':digest(reference.sql),'scorer':SCORER,'limits':asdict(tools.limits)}

    def _execute(self, sql):
        if (self.path.stat().st_size, self.path.stat().st_mtime_ns) != self.stamp:
            raise EvaluationLimitExceeded('Database changed during evaluation; cached results cannot be used')
        reusable = not re.search(r"\b(random|randomblob|current_timestamp|current_time|current_date|changes|total_changes|last_insert_rowid)\b|['\"]now['\"]", sql, re.I)
        if reusable and sql in self.cache:
            return self.cache[sql], True
        result = self.tools.full_result_details(self.reference.db_id, sql)
        if reusable: self.cache[sql] = result
        return result, False

    def score(self, sql, label):
        started = time.monotonic()
        record = {'label':label,'candidate_sql_sha256':digest(sql), 'status':'error','score':None}
        self.emit({'event':'evaluation_case_started','evaluation_case':label,
                   'candidate_sql_sha256':record['candidate_sql_sha256']})
        phase = 'reference'
        try:
            gold, gold_cached = self._execute(self.reference.sql)
            record.update(reference=summarize(gold), reference_reused=gold_cached)
            phase = 'candidate'
            predicted, cached = self._execute(sql)
            record.update(candidate=summarize(predicted), candidate_reused=cached,
                status='scored', score=float(predicted['rows']==gold['rows']),
                missing_rows=len(gold['rows']-predicted['rows']), extra_rows=len(predicted['rows']-gold['rows']),
                missing_examples=examples(gold['rows']-predicted['rows']),
                extra_examples=examples(predicted['rows']-gold['rows']),
                reason='match' if predicted['rows']==gold['rows'] else 'result_mismatch')
        except (sqlite3.Error, ValueError, EvaluationLimitExceeded) as error:
            # Infrastructure/limits and reference failures are not wrong answers.
            infrastructure = isinstance(error, EvaluationLimitExceeded) or any(
                word in str(error).lower() for word in ('interrupted','locked','disk','i/o','unable to open','malformed'))
            if phase=='candidate' and not infrastructure:
                record.update(status='scored',score=0.0,reason='candidate_sql_error')
            else:
                record['reason'] = phase+'_evaluation_error'
            record.update(error_type=type(error).__name__,error=str(error)[:500])
        record['elapsed_seconds'] = time.monotonic()-started
        self.comparisons.append(record)
        if self.artifact:
            self.artifact.write_text(json.dumps({'model_input':False,**self.identity,
                'comparisons':self.comparisons},ensure_ascii=False,indent=2),encoding='utf8')
        # Raw gold, result rows and comparison examples stay out of workflow trace.
        self.emit({'event':'evaluation_case_completed', 'evaluation_case':label,
            **{k:record[k] for k in ('status','score','reason','elapsed_seconds','candidate_sql_sha256')},
            'reference_reused':record.get('reference_reused',False),
            'candidate_reused':record.get('candidate_reused',False),
            'evidence_file':self.artifact.name if self.artifact else None})
        return record
