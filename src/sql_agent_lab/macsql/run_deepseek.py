"""Six-question local API pilot; isolated workers enforce a whole-question deadline."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from .context import ContextPolicy
from .dataset import load_bird_jsonl, score_sql
from .deepseek_api import DeepSeekModel, Ledger, MODEL, read_events
from .fetch_bird import sha256
from .live_ui import ObservedTools
from .metadata import load_metadata
from .reference_baseline import ReferenceMacSQLAgent
from .reference_core import JsonlTrace, WorkflowConfig, ROLES
from .tools import SQLiteTools, ToolLimits
from .evaluation_audit import EvaluationSession
from .trace_support import TraceEmitter

IDS = ('118', '165', '129', '308', '333', '203')


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def worker(args):
    qid = args.ids[0]
    output = args.output / ('q' + qid)
    if output.exists():
        raise ValueError('Existing question directory: refusing duplicate paid execution')
    output.mkdir()
    started = time.monotonic()
    trace = JsonlTrace(output / 'trace.jsonl')
    emit = TraceEmitter(trace, output.name, qid, started=started)
    result = dict(status='failed', score=None, question_id=qid)
    source = json.loads((args.data / 'source-lock.json').read_text())
    paths = {db: args.data / entry['path'] for db, entry in source['databases'].items()}
    question, reference = load_bird_jsonl(args.data / 'mini_dev_sqlite.jsonl', paths, question_ids=[qid])[0]
    manifest = dict(question=asdict(question), mode='deepseek_api', model={'name': MODEL},
                    thinking=False, semantic_review=getattr(args, 'semantic_review', False), schema_retrieval=True,
                    workflow_mode='optional_semantic_review' if getattr(args, 'semantic_review', False) else 'execution_repair_only',
                    protocol='../protocol.json', reference_answers_in_model_inputs=False)
    write(output / 'manifest.json', manifest)
    copied = output / (question.db_id + '.sqlite')
    try:
        shutil.copy2(paths[question.db_id], copied)
        expected_hash = source['databases'][question.db_id]['sha256']
        if sha256(copied) != expected_hash:
            raise ValueError('Database hash mismatch')
        raw = SQLiteTools({question.db_id: copied}, ToolLimits(timeout_seconds=30),
                          metadata=load_metadata(args.metadata))
        observed = ObservedTools(raw, emit, threading.Event())
        model = DeepSeekModel(os.environ.get('DEEPSEEK_API_KEY'), Ledger(args.output / 'api-calls.jsonl',
            batch_cap=getattr(args, 'batch_cap', 80000),
            question_cap=getattr(args, 'question_cap', 20000),
            max_calls=5 if getattr(args, 'semantic_review', False) else 3), qid)
        config = WorkflowConfig(observed, model, trace=emit, max_repairs=1,
            context=ContextPolicy(input_budget=14000, context_window=16384),
            schema_retrieval=True, value_lookup=True,
            semantic_review=getattr(args, 'semantic_review', False), semantic_output_tokens=1600)
        agent = ReferenceMacSQLAgent({r: MODEL for r in ROLES}, config=config)
        sql, feedback = agent.run_with_feedback(question)
        result.update(status='completed' if feedback['status'] == 'ok' else 'failed', sql=sql, feedback=feedback)
        # Gold SQL is used only after the graph ends, never in role messages.
        emit({'event': 'evaluation_started'})
        try:
            evaluator = EvaluationSession(raw,reference,expected_hash,artifact=output/'evaluation-evidence.json',emit=emit)
            evaluation = evaluator.score(sql,'submitted')
            result.update(score=evaluation['score'],evaluation_status=evaluation['status'],
                          evaluation_evidence_file='evaluation-evidence.json')
            if evaluation['status']=='error': result['status']='evaluation_failed'
            if getattr(args, 'audit_stages', False):
                saved_events = read_events(output / 'trace.jsonl')
                stage_scores = {}
                for event_name, label in [('query_plan', 'initial'), ('semantic_review_started', 'before_semantic')]:
                    item = next((e for e in saved_events if e['event'] == event_name and e.get('sql')), None)
                    if item:
                        stage_scores[label] = evaluator.score(item['sql'], label)
                final_event = next((e for e in reversed(saved_events) if e['event'] == 'workflow_completed'), {})
                from .run_deepseek_review import evaluate_suggestion
                result.update(stage_evaluations=stage_scores,
                    suggestion_evaluation=evaluate_suggestion(raw, reference, final_event, evaluator=evaluator))
        except Exception as error:
            result.update(evaluation_status='failed', evaluation_error_type=type(error).__name__)
        emit({'event': 'evaluation_completed', 'score': result['score'],
              'status': result['evaluation_status']})
        result['database_unchanged'] = sha256(copied) == expected_hash
    except Exception as error:
        result.update(error_type=type(error).__name__, error=str(error))
    events = read_events(output / 'trace.jsonl')
    finished = next((e for e in reversed(events) if e['event'] == 'workflow_completed'), {})
    result.update(semantic_status=finished.get('semantic_status', 'not_completed'),
                  semantic_metrics=finished.get('semantic_metrics', {}),
                  output_type_check=finished.get('output_type_check', {'status': 'unverified'}),
                  elapsed_seconds=time.monotonic() - started)
    calls = [e for e in read_events(args.output / 'api-calls.jsonl')
             if e['event'] == 'request_finished' and e['question_id'] == qid]
    result.update(cloud_api_calls=len(calls), input_tokens=sum(e.get('input_tokens', 0) for e in calls),
                  output_tokens=sum(e.get('output_tokens', 0) for e in calls),
                  usage_complete=all(e['usage_known'] for e in calls),
                  estimated_cost_usd_peak_no_cache=sum(e.get('estimated_cost_usd_peak_no_cache', 0) for e in calls))
    write(output / 'result.json', result)
    print(json.dumps({k: result.get(k) for k in ('question_id', 'status', 'score', 'semantic_status',
          'cloud_api_calls', 'input_tokens', 'output_tokens', 'elapsed_seconds', 'error')}, ensure_ascii=False), flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=Path, required=True)
    p.add_argument('--metadata', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--smallset', type=Path, required=True)
    p.add_argument('--ids', nargs='+', required=True, choices=IDS)
    p.add_argument('--worker', action='store_true')
    p.add_argument('--semantic-review', action='store_true',
                   help='Optional experimental audit: up to two extra model calls per question')
    args = p.parse_args()
    args.output = args.output.resolve()
    if args.worker:
        worker(args)
        return
    if len(args.ids) != len(set(args.ids)):
        p.error('Duplicate question IDs')
    if not os.environ.get('DEEPSEEK_API_KEY'):
        p.error('DEEPSEEK_API_KEY is not available; secret is never logged')
    checkout = Path(__file__).resolve().parents[3]
    if args.output == checkout or checkout in args.output.parents:
        p.error('Keep outputs outside the source checkout')
    source = json.loads((args.data / 'source-lock.json').read_text())
    subset = json.loads((args.smallset / 'manifest.json').read_text())
    if sha256(args.data / 'mini_dev_sqlite.jsonl') != source['annotations_sha256'] or source['annotations_sha256'] != subset['source_sha256']:
        p.error('Dataset checksum mismatch')
    if sha256(args.smallset / 'core6.jsonl') != subset['hashes']['core6.jsonl'] or tuple(subset['core_question_ids']) != IDS:
        p.error('Smallset changed')
    files = {str(f.relative_to(checkout)): sha256(f) for f in Path(__file__).parent.glob('*.py')}
    protocol = dict(model=MODEL, documented_version='DeepSeek-V4.1-Flash-1017',
        model_version_source='https://api-docs.deepseek.com/quick_start/pricing/',
        thinking=False, data_sha256=source['annotations_sha256'], metadata_sha256=sha256(args.metadata),
        source_hashes=files, ids=list(args.ids), schema_retrieval=True, value_lookup=True,
        semantic_review=args.semantic_review,
        workflow_mode='optional_semantic_review' if args.semantic_review else 'execution_repair_only',
        max_repairs=1, max_model_requests_per_question=5 if args.semantic_review else 3,
        question_token_cap=20000, batch_token_cap=80000,
        token_reservation='UTF-8 bytes plus chat framing plus maximum output; reconcile provider usage',
        input_byte_budget=14000, context_window=16384, generation_output_limit=1000, complex_generation_output_limit=2400, generation_budget_policy="input_complexity_v1", max_schema_refreshes=1, semantic_output_limit=1600, requirements_output_limit=800,
        request_deadline_seconds=120, question_deadline_seconds=300, automatic_retries=0,
        prices_usd_per_million_peak={'input_uncached': .30, 'input_cached': .006, 'output': 1.20},
        cost_note='Report peak/no-cache upper estimate; actual tariff may be half and cache hits cheaper',
        reference_answers_in_model_inputs=False, agentopt_proxy_accounting_verified=False)
    args.output.mkdir(parents=True, exist_ok=True)
    protocol_path = args.output / 'protocol.json'
    if protocol_path.exists() and json.loads(protocol_path.read_text(encoding='utf8')) != protocol:
        p.error('Protocol/source changed: use a new batch output directory')
    write(protocol_path, protocol)
    for qid in args.ids:
        if (args.output / ('q' + qid)).exists():
            p.error('Requested question already exists; refusing duplicate paid execution')
    # Only one controlling invocation can operate on this ledger at a time.
    lock_path = args.output / '.running'
    with lock_path.open('x') as lock:
        lock.write(str(os.getpid()))
    try:
        for qid in args.ids:
            if Ledger(args.output / 'api-calls.jsonl').totals()[2]:
                print('STOP: previous request has unknown usage', flush=True)
                break
            command = [sys.executable, '-m', __package__ + '.run_deepseek', '--worker',
                       '--data', str(args.data), '--metadata', str(args.metadata), '--output', str(args.output),
                       '--smallset', str(args.smallset), '--ids', qid]
            if args.semantic_review:
                command.append('--semantic-review')
            try:
                completed = subprocess.run(command, cwd=checkout, timeout=300)
            except subprocess.TimeoutExpired:
                write(args.output / ('q' + qid) / 'result.json',
                      dict(status='timed_out', score=None, question_id=qid, elapsed_seconds=300,
                           note='Worker terminated; sent requests may still be billed; no automatic retry'))
                print('STOP: whole-question deadline reached', flush=True)
                break
            if completed.returncode:
                print('STOP: worker failed', flush=True)
                break
    finally:
        lock_path.unlink()
    rows = [json.loads(f.read_text(encoding='utf8')) for f in args.output.glob('q*/result.json')]
    write(args.output / 'summary.json', dict(results=rows, correct=sum(r.get('score') == 1 for r in rows),
        scored=sum(r.get('score') is not None for r in rows), planned=6,
        updated_utc=datetime.now(timezone.utc).isoformat()))


if __name__ == '__main__':
    main()
