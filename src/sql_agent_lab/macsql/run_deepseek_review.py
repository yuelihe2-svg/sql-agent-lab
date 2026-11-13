"""Two-stage paid review of a saved SQL; no upstream generation or automatic retries."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
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
from .semantic_review import CONTRACT
from .tools import SQLiteTools, ToolLimits
from .evaluation_audit import EvaluationSession
from .trace_support import TraceEmitter


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def evaluate_suggestion(tools, reference, update, *, evaluator=None):
    """Separate post-model evaluation. Never replace the submitted/original SQL."""
    proposal = update.get('output_projection_suggestion') or update.get('semantic_suggestion')
    if not proposal or not proposal.get('sql'):
        return {'status':'not_generated','score':None,'sql':None,'adopted':False}
    started = time.monotonic()
    sql = proposal['sql']
    feedback = tools.compile_sql_readonly(reference.db_id, sql)
    if feedback['status'] == 'ok':
        feedback = tools.execute_sql_readonly(reference.db_id, sql)
    evaluator = evaluator or EvaluationSession(tools, reference, sha256(tools.databases[reference.db_id]))
    evaluation = evaluator.score(sql, 'suggestion')
    return {'status':evaluation['status'],'score':evaluation['score'],'sql':sql,'adopted':False,
            'evaluation_reason':evaluation['reason'],
            'feedback':feedback,'elapsed_seconds':time.monotonic()-started}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--metadata',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--execute',action='store_true',help='Authorize at most two model requests; otherwise prepare only')
    p.add_argument('--suggestions-only',action='store_true',help='Keep original SQL; evaluate proposed SQL separately after requests')
    args=p.parse_args(); out=args.output.resolve()
    checkout=Path(__file__).resolve().parents[3]
    if checkout==out or checkout in out.parents: p.error('Keep outputs outside checkout')
    if out.exists(): p.error('Output already exists; never overwrite or repeat a paid run')
    if args.execute and not os.environ.get('DEEPSEEK_API_KEY'): p.error('API key unavailable; value is never logged')
    baseline_manifest=json.loads((args.baseline/'manifest.json').read_text(encoding='utf8'))
    old_events=read_events(args.baseline/'trace.jsonl')
    original=next(e['sql'] for e in old_events if e['event']=='semantic_review_started')
    qid=baseline_manifest['question']['question_id']
    lock=json.loads((args.data/'source-lock.json').read_text(encoding='utf8'))
    if sha256(args.data/'mini_dev_sqlite.jsonl')!=lock['annotations_sha256']: p.error('Dataset changed')
    paths={db:args.data/e['path'] for db,e in lock['databases'].items()}
    question,reference=load_bird_jsonl(args.data/'mini_dev_sqlite.jsonl',paths,question_ids=[qid])[0]
    if asdict(question)!=baseline_manifest['question']: p.error('Question differs from saved run')
    out.mkdir(parents=True)
    started=time.monotonic();trace=JsonlTrace(out/'trace.jsonl')
    emit = TraceEmitter(trace, out.name, qid, started=started)
    protocol={'kind':'fixed_sql_semantic_review','model':MODEL,'contract':CONTRACT,
        'baseline_path':str(args.baseline.resolve()),'baseline_sql_sha256':__import__('hashlib').sha256(original.encode()).hexdigest(),
        'baseline_trace_sha256':sha256(args.baseline/'trace.jsonl'),
        'dataset_sha256':lock['annotations_sha256'],'metadata_sha256':sha256(args.metadata),
        'max_model_requests':2,'requirements_output_limit':800,'output_limit':1600,'request_deadline_seconds':120,
        'input_byte_budget':14000,'context_window':16384,'token_reservation_cap':20000,
        'automatic_retries':0,'thinking':False,'reference_answers_in_model_inputs':False,
        'schema_documentation':True,'schema_samples':False,'trace_schema_version':2,
        'suggestions_only':args.suggestions_only,
        'source_hashes':{f.name:sha256(f) for f in Path(__file__).parent.glob('*.py')}}
    write(out/'protocol.json',protocol)
    snapshot=out/'source-snapshot';snapshot.mkdir()
    for f in Path(__file__).parent.glob('*.py'): shutil.copy2(f,snapshot/f.name)
    manifest={'question':asdict(question),'mode':'paired_candidate_audit','model':{'name':MODEL},
        'created_at':datetime.now(timezone.utc).isoformat(),'baseline_sql':original,
        'semantic_review':True,'semantic_review_contract':CONTRACT,'baseline_path':protocol['baseline_path'],
        'suggestions_only':args.suggestions_only,
        'schema_retrieval':False,'upstream_reused':True,'simulated':not args.execute}
    write(out/'manifest.json',manifest)
    copied=out/(question.db_id+'.sqlite');shutil.copy2(paths[question.db_id],copied)
    expected=lock['databases'][question.db_id]['sha256']
    if sha256(copied)!=expected: raise ValueError('Database copy hash mismatch')
    raw=SQLiteTools({question.db_id:copied},ToolLimits(timeout_seconds=30),metadata=load_metadata(args.metadata))
    tools=ObservedTools(raw,emit,threading.Event())
    ledger=Ledger(out/'api-calls.jsonl',batch_cap=20000,question_cap=20000,max_calls=2)
    captured={}
    def prepare_only(**kw):
        captured.update(messages=kw['messages'],max_output_tokens=kw['max_output_tokens'])
        raise RuntimeError('Preparation only; no model request')
    model=DeepSeekModel(os.environ.get('DEEPSEEK_API_KEY'),ledger,qid) if args.execute else prepare_only
    agent=ReferenceMacSQLAgent({r:MODEL for r in ROLES},config=WorkflowConfig(tools,model,trace=emit,
        max_repairs=0,semantic_review=True,semantic_suggestion_only=args.suggestions_only,semantic_output_tokens=1600,
        context=ContextPolicy(input_budget=14000,context_window=16384)))
    result={'status':'failed','question_id':qid,'baseline_score':None,'score':None,'sql':original}
    try:
        emit({'event':'workflow_started','mode':'paired_candidate_audit','upstream_reused':True})
        schema=tools.load_schema(question.db_id, enrich=True, sample_values=False)
        feedback=tools.compile_sql_readonly(question.db_id,original)
        if feedback['status']!='ok': raise ValueError('Saved SQL no longer compiles')
        feedback=tools.execute_sql_readonly(question.db_id,original)
        if feedback['status']!='ok': raise ValueError('Saved SQL no longer executes')
        state={'run_id':out.name,'question':question,'sql':original,'full_schema':schema,'feedback':feedback}
        state['output_type_report']=agent._check_output(state,original,feedback)
        review_started=time.monotonic()
        update=agent._semantic_review(state)
        review_seconds=time.monotonic()-review_started
        if not args.execute:
            write(out/'preflight.json',{'prepared':bool(captured),'new_model_calls':0,
                'input_bytes':sum(len(m['content'].encode()) for m in captured.get('messages',[])),
                'output_limit':captured.get('max_output_tokens'),'error':update.get('semantic_report',{}).get('error')})
            return
        sql=update.get('sql',original); after_feedback=update.get('feedback',feedback)
        if args.suggestions_only and sql != original:
            raise RuntimeError('Suggestion-only invariant violated: submitted SQL changed')
        result.update(update,sql=sql,status='completed',feedback=after_feedback,
            added_review_seconds=review_seconds,sql_changed=sql!=original,
            output_type_check=update.get('output_type_report',state['output_type_report']))
        emit({'event':'workflow_completed','evaluation_pending':True,
              **{k:result.get(k) for k in ('sql','semantic_status','semantic_metrics','semantic_validation','output_type_check','output_columns_report','output_requirements','output_requirements_assessment','output_requirements_protocol_warnings')}})
        # Paired scores are computed only after the model has finished.
        emit({'event':'evaluation_started'})
        eval_started=time.monotonic()
        evaluator=EvaluationSession(raw,reference,expected,artifact=out/'evaluation-evidence.json',emit=emit)
        baseline=evaluator.score(original,'baseline')
        submitted=evaluator.score(sql,'submitted')
        result['baseline_score']=baseline['score']
        result['score']=submitted['score']
        result['evaluation_evidence_file']='evaluation-evidence.json'
        if args.suggestions_only:
            emit({'event':'suggestion_evaluation_started'})
            result['suggestion_evaluation']=evaluate_suggestion(tools,reference,update,evaluator=evaluator)
            emit({'event':'suggestion_evaluation_completed',**result['suggestion_evaluation']})
        result['evaluation_seconds']=time.monotonic()-eval_started
        result['evaluation_status']='scored' if baseline['status']==submitted['status']=='scored' else 'error'
        if result['evaluation_status']=='error': result['status']='evaluation_failed'
        emit({'event':'evaluation_completed','baseline_score':result['baseline_score'],'score':result['score']})
    except Exception as error:
        result.update(error_type=type(error).__name__,error=str(error))
    finally:
        if args.execute:
            calls=read_events(out/'api-calls.jsonl');ends=[e for e in calls if e['event']=='request_finished']
            known=bool(ends) and all(e['usage_known'] for e in ends)
            unchanged=sha256(copied)==expected
            result.update(elapsed_seconds=time.monotonic()-started,database_unchanged=unchanged,
                cloud_api_calls=sum(e['event']=='request_started' for e in calls),
                input_tokens=sum(e['input_tokens'] for e in ends) if known else None,
                output_tokens=sum(e['output_tokens'] for e in ends) if known else None,
                usage_complete=known,api_seconds=sum(e['elapsed_seconds'] for e in ends),
                estimated_cost_usd_peak_no_cache=sum(e.get('estimated_cost_usd_peak_no_cache',0) for e in ends) if known else None)
            emit({'event':'run_finished',**result});write(out/'result.json',result)
            print(json.dumps({k:result.get(k) for k in ('status','baseline_score','score','semantic_status','cloud_api_calls','input_tokens','output_tokens','api_seconds','added_review_seconds','elapsed_seconds','error')},ensure_ascii=False))


if __name__=='__main__': main()
