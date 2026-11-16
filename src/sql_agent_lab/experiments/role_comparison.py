"""Paired A/B/C DeepSeek experiment. Prepare offline, then explicitly run paid jobs.

A: one general-role generation. B: same generation plus feedback-led repair.
C: unchanged ReferenceMacSQLAgent. All share read-only tools and scoring.
Artifacts, questions, traces and reports must stay outside the checkout.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import time

from sql_agent_lab.macsql.context import ContextPolicy
from sql_agent_lab.macsql.dataset import load_bird_jsonl
from sql_agent_lab.macsql.deepseek_api import DeepSeekModel, Ledger, MODEL, read_events
from sql_agent_lab.macsql.evaluation_audit import EvaluationSession, SCORER
from sql_agent_lab.macsql.fetch_bird import sha256
from sql_agent_lab.macsql.live_ui import ObservedTools
from sql_agent_lab.macsql.metadata import load_metadata
from sql_agent_lab.macsql.reference_baseline import (
    ReferenceMacSQLAgent, question_literals, retrieval_hints,
)
from sql_agent_lab.macsql.reference_core import JsonlTrace, ROLES, WorkflowConfig
from sql_agent_lab.macsql.result_risks import RESULT_SEMANTICS_GUIDANCE
from sql_agent_lab.macsql.trace_support import TraceEmitter
from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits

CHECKOUT = Path(__file__).resolve().parents[3]
SETTINGS = ('A', 'B', 'C')
QUESTION_CAP = 50_000
JOB_DEADLINE = 420


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    temporary.replace(path)


class SingleRoleAgent(ReferenceMacSQLAgent):
    """Use one general prompt and full schema, with optional error feedback.

Reuse existing graph checks, repair acceptance, context compaction and budgets.
No model Selector; no separate repair persona. No reference object is accepted.
"""

    def _decomposer_prompt(self, question, evidence, rendered, hints=''):
        return (
            'Answer the question by writing a read-only SQLite query. Use exact schema '
            'identifiers and the supplied evidence. You may reason briefly if helpful, '
            'but return one complete final query in a ```sql fenced block. '
            'Prioritize completing SQL over explanations. Return requested quantities '
            'as numeric values; preserve identifiers, postal codes, phones and leading '
            'zeroes as stored. Do not cast malformed values to zero.\n'
            + RESULT_SEMANTICS_GUIDANCE + '\nSchema:\n' + rendered
            + '\nQuestion: ' + question + '\nEvidence: ' + evidence
            + ('\n' + hints if hints else '')
        )

    def _refiner_prompt(self, question, evidence, rendered, sql, feedback, hints=''):
        return self._decomposer_prompt(question, evidence, rendered, hints) + (
            '\nYour previous SQL:\n```sql\n' + sql + '\n```\n'
            'Execution/type-check feedback:\n' + json.dumps(feedback, ensure_ascii=False)
            + '\nRevise your query to address this feedback while preserving the '
            'requested conditions and output columns. Return one complete SQL block. '
            'Empty results and NULL alone are not errors.'
        )

    def _ask_reference(self, state, role, messages, budget):
        budget = {**budget, 'purpose': 'single_role_repair' if role == 'refiner' else 'single_role_generation'}
        # Both generation and revision use the same model role/system instruction.
        return super()._ask_reference(state, 'decomposer', messages, budget)

    def _select(self, state):
        q = state['question']
        full = self.config.tools.load_schema(q.db_id, enrich=True)
        self._event(state, 'schema_loaded', schema=full)
        hints = ''
        if self.config.schema_retrieval:
            matches = self.config.tools.search_schema(q.db_id, q.question + '\n' + q.evidence,
                top_k=self.config.retrieval_top_k, schema=full)
            lookups = []
            if self.config.value_lookup:
                for literal in question_literals(q.question + '\n' + q.evidence):
                    for item in matches:
                        kind = item['type'].upper()
                        if kind and not any(t in kind for t in ('TEXT', 'CHAR', 'CLOB')):
                            continue
                        lookups.append(self.config.tools.lookup_values(
                            q.db_id, item['table'], item['column'], literal))
                        if len(lookups) >= 2:
                            break
                    if len(lookups) >= 2:
                        break
            hints = retrieval_hints(matches, lookups)
            self._event(state, 'schema_retrieved', matches=matches, value_lookups=lookups,
                        hint_chars=len(hints))
        self._event(state, 'schema_selected', selected_schema=full,
                    selector_invoked=False, pruned=False, policy='full_schema_no_model_selector')
        return dict(selected_schema=full, full_schema=full, schema_refresh_count=0,
                    coverage={}, retrieval_hints=hints)


def make_agent(setting, tools, model, emit):
    if setting not in SETTINGS:
        raise ValueError('Unknown setting')
    config = WorkflowConfig(tools, model, trace=emit,
        max_repairs={'A': 0, 'B': 2, 'C': 1}[setting],
        context=ContextPolicy(input_budget=14000, context_window=16384),
        schema_retrieval=True, value_lookup=True, semantic_review=False)
    cls = ReferenceMacSQLAgent if setting == 'C' else SingleRoleAgent
    return cls({r: MODEL for r in ROLES}, config=config)


def known_fixture_ids(rows):
    ids = {'118', '165', '129', '308', '333', '203'}
    by_text = {r['question']: str(r['question_id']) for r in rows}
    def visit(value):
        if isinstance(value, dict):
            if 'question_id' in value:
                ids.add(str(value['question_id']))
            for k, v in value.items():
                if str(k).isdigit():
                    ids.add(str(k))
                visit(v)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and value in by_text:
            ids.add(by_text[value])
    for path in (CHECKOUT/'src/sql_agent_lab/macsql/fixtures').glob('*.json'):
        visit(json.loads(path.read_text(encoding='utf8')))
    return ids & {str(r['question_id']) for r in rows}


def sample_rows(rows, excluded, n, seed):
    """Proportional largest-remainder allocation by database and supplied difficulty."""
    groups = defaultdict(list)
    for row in rows:
        if str(row['question_id']) not in excluded:
            groups[(row['db_id'], row.get('difficulty', 'unknown'))].append(row)
    total = sum(map(len, groups.values()))
    if not 0 < n <= total:
        raise ValueError('Insufficient eligible questions')
    quotas = {k: n * len(v) // total for k, v in groups.items()}
    ranked = sorted(groups, key=lambda k: (-(n * len(groups[k]) % total), k))
    for k in ranked[:n - sum(quotas.values())]:
        quotas[k] += 1
    rng = random.Random(seed)
    chosen = []
    for k in sorted(groups):
        candidates = sorted(groups[k], key=lambda r: str(r['question_id']))
        chosen.extend(rng.sample(candidates, quotas[k]))
    rng.shuffle(chosen)
    return chosen


def source_hashes():
    return {str(p.relative_to(CHECKOUT)): sha256(p)
            for p in sorted((CHECKOUT/'src/sql_agent_lab').rglob('*.py'))}


def prepare(args):
    if args.output.exists():
        raise ValueError('Use a new output directory; protocol is immutable')
    lock = json.loads((args.data/'source-lock.json').read_text(encoding='utf8'))
    annotations = args.data/'mini_dev_sqlite.jsonl'
    if sha256(annotations) != lock['annotations_sha256']:
        raise ValueError('Annotation checksum mismatch')
    rows = [json.loads(line) for line in annotations.read_text(encoding='utf8').splitlines() if line.strip()]
    excluded = known_fixture_ids(rows)
    chosen = sample_rows(rows, excluded, args.count, args.seed)
    dbs = sorted({r['db_id'] for r in chosen})
    for db in dbs:
        if sha256(args.data/lock['databases'][db]['path']) != lock['databases'][db]['sha256']:
            raise ValueError('Database checksum mismatch: ' + db)
    args.output.mkdir(parents=True)
    protocol = dict(created_utc=datetime.now(timezone.utc).isoformat(),
        git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=CHECKOUT,text=True).strip(),
        source_hashes=source_hashes(), data=str(args.data), metadata=str(args.metadata),
        data_sha256=lock['annotations_sha256'], metadata_sha256=sha256(args.metadata),
        database_hashes={db:lock['databases'][db]['sha256'] for db in dbs},
        model=MODEL, documented_version='DeepSeek-V4.1-Flash-1017',
        pricing_source='https://api-docs.deepseek.com/quick_start/pricing/',
        temperature=0, thinking=False, seed_for_sampling=args.seed,
        model_seed_supported=False, repetitions=1, settings=list(SETTINGS),
        count_per_setting=args.count, excluded_known_fixture_ids=sorted(excluded),
        ids=[str(r['question_id']) for r in chosen],
        strata_counts=dict(Counter(r['db_id']+' / '+r.get('difficulty','unknown') for r in chosen)),
        selection='proportional database/difficulty strata; fixed seed; no outcome selection',
        schema_policy={'A':'full schema with existing progressive compaction',
            'B':'same as A', 'C':'unchanged conditional model Selector'},
        max_calls={'A':1,'B':3,'C':3}, max_repairs={'A':0,'B':2,'C':1},
        repair_policy='existing execution/type gates; stop on unchanged or rejected repair',
        question_token_cap=QUESTION_CAP, total_token_cap=args.count*3*QUESTION_CAP,
        conservative_cost_upper_usd=args.count*3*QUESTION_CAP*1.2/1e6,
        pricing='peak/no-cache estimate: input 0.30 USD/M; output 1.20 USD/M, not a bill',
        context={'input_byte_budget':14000,'context_window':16384,'base_output':1000,'complex_output':2400},
        semantic_review=False, automatic_retries=0, reference_answers_in_model_inputs=False,
        request_timeout_seconds=120, job_deadline_seconds=JOB_DEADLINE,
        scorer=SCORER, sql_timeout_seconds=30, planned_concurrency=args.concurrency,
        order='rotate ABC/BCA/CAB per question; sequential arms within question',
        failure_policy='generation failures remain in denominator; evaluation failures unresolved; unknown API usage stops new jobs')
    write(args.output/'protocol.json',protocol)
    # No reference SQL in the frozen selection file.
    write(args.output/'questions.json',[{k:r.get(k) for k in ('question_id','db_id','question','evidence','difficulty')} for r in chosen])
    print(json.dumps({'prepared':str(args.output),'questions':len(chosen),'excluded':len(excluded),
                      'strata':protocol['strata_counts'],'max_cost_usd':protocol['conservative_cost_upper_usd']}),flush=True)


def verify_protocol(args, protocol):
    if source_hashes() != protocol['source_hashes']:
        raise ValueError('Source changed after protocol freeze')
    if sha256(Path(protocol['data'])/'mini_dev_sqlite.jsonl') != protocol['data_sha256']:
        raise ValueError('Annotations changed')
    if sha256(Path(protocol['metadata'])) != protocol['metadata_sha256']:
        raise ValueError('Metadata changed')


def worker(args):
    protocol = json.loads((args.output/'protocol.json').read_text(encoding='utf8'))
    if args.qid not in protocol['ids'] or args.setting not in SETTINGS:
        raise ValueError('Job is outside the frozen selection')
    target = args.output/args.setting/('q'+args.qid)
    target.mkdir(parents=True, exist_ok=False)
    begin = time.monotonic()
    emit = TraceEmitter(JsonlTrace(target/'trace.jsonl'),args.setting+'-q'+args.qid,args.qid,started=begin)
    data = Path(protocol['data'])
    lock = json.loads((data/'source-lock.json').read_text(encoding='utf8'))
    databases = {k:data/v['path'] for k,v in lock['databases'].items()}
    question, reference = load_bird_jsonl(data/'mini_dev_sqlite.jsonl',databases,question_ids=[args.qid])[0]
    write(target/'manifest.json',dict(setting=args.setting,question=asdict(question),model=MODEL,
        protocol='../../protocol.json',reference_answers_in_model_inputs=False))
    result = dict(setting=args.setting,question_id=args.qid,db_id=question.db_id,
        status='failed',score=0.0,evaluation_status='not_run',sql=None)
    ledger = Ledger(target/'api-calls.jsonl',batch_cap=QUESTION_CAP,question_cap=QUESTION_CAP,
                    max_calls=protocol['max_calls'][args.setting])
    try:
        raw = SQLiteTools({question.db_id:databases[question.db_id]},ToolLimits(timeout_seconds=30),
                          metadata=load_metadata(Path(protocol['metadata'])))
        observed = ObservedTools(raw,emit,threading.Event())
        model = DeepSeekModel(os.environ.get('DEEPSEEK_API_KEY'),ledger,args.qid)
        sql = make_agent(args.setting,observed,model,emit).run(question)
        result['sql'] = sql
        result['agent_seconds'] = time.monotonic()-begin
        feedback = raw.compile_sql_readonly(question.db_id,sql)
        if feedback['status']=='ok':
            feedback = raw.execute_sql_readonly(question.db_id,sql)
        result.update(feedback=feedback,status='completed' if feedback['status']=='ok' else 'failed')
        evaluator = EvaluationSession(raw,reference,protocol['database_hashes'][question.db_id],
            artifact=target/'evaluation-evidence.json',emit=emit)
        scored = evaluator.score(sql,'final')
        result.update(score=scored['score'],evaluation_status=scored['status'],
                      evaluation_reason=scored['reason'],evaluation_seconds=scored['elapsed_seconds'])
    except Exception as error:
        # Provider exceptions are sanitized by the shared adapter; never dump headers/env.
        result.update(error_type=type(error).__name__,error_code=getattr(error,'code',type(error).__name__))
        if result['sql'] is not None:
            result.update(score=None,evaluation_status='error')
    events = read_events(target/'trace.jsonl')
    final = next((e for e in reversed(events) if e['event']=='workflow_completed'),{})
    selected = next((e for e in events if e['event']=='schema_selected'),{})
    calls = read_events(target/'api-calls.jsonl')
    starts = [e for e in calls if e['event']=='request_started']
    ends = [e for e in calls if e['event']=='request_finished']
    result.update(elapsed_seconds=time.monotonic()-begin,
        agent_seconds=result.get('agent_seconds',time.monotonic()-begin),
        cloud_api_calls=len(starts),usage_complete=not ledger.totals()[2],
        input_tokens=sum(e.get('input_tokens',0) for e in ends),
        output_tokens=sum(e.get('output_tokens',0) for e in ends),
        estimated_cost_usd_peak_no_cache=sum(e.get('estimated_cost_usd_peak_no_cache',0) for e in ends),
        role_calls=dict(Counter(e['role'] for e in starts)),
        returned_models=sorted({e.get('returned_model','unknown') for e in ends}),
        selector_invoked=selected.get('selector_invoked',False),
        repair_count=final.get('repair_count',sum(e['event']=='sql_repaired' for e in events)),
        semantic_status=final.get('semantic_status','not_completed'),
        output_type_check=final.get('output_type_check',{}))
    write(target/'result.json',result)
    print(json.dumps({k:result.get(k) for k in ('setting','question_id','status','score','cloud_api_calls','elapsed_seconds','error_code')}),flush=True)


def read_results(output):
    return [json.loads(p.read_text(encoding='utf8')) for p in sorted(output.glob('[ABC]/q*/result.json'))]


def run(args):
    protocol = json.loads((args.output/'protocol.json').read_text(encoding='utf8'))
    verify_protocol(args,protocol)
    if not os.environ.get('DEEPSEEK_API_KEY'):
        raise ValueError('DEEPSEEK_API_KEY unavailable; value is never logged')
    if any(not r.get('usage_complete',False) for r in read_results(args.output)):
        raise ValueError('Previous usage unknown; refusing further paid calls')
    stop = threading.Event()
    question_db={str(q['question_id']):q['db_id'] for q in
                 json.loads((args.output/'questions.json').read_text(encoding='utf8'))}
    def group(index,qid):
        rotation = index % 3
        for setting in SETTINGS[rotation:]+SETTINGS[:rotation]:
            if stop.is_set():
                return
            directory = args.output/setting/('q'+qid)
            if directory.exists():
                if (directory/'result.json').exists():
                    continue
                stop.set()
                raise ValueError('Existing unfinished job; never retry paid jobs automatically')
            command = [sys.executable,'-B','-m','sql_agent_lab.experiments.role_comparison','worker','--output',str(args.output),
                       '--setting',setting,'--qid',qid]
            log = args.output/(setting+'-q'+qid+'.worker.log')
            try:
                with log.open('w',encoding='utf8') as stream:
                    child = subprocess.run(command,cwd=CHECKOUT,stdout=stream,stderr=subprocess.STDOUT,
                        timeout=JOB_DEADLINE,env={**os.environ,'LANGCHAIN_TRACING_V2':'false','LANGSMITH_TRACING':'false'})
                if child.returncode or not (directory/'result.json').exists():
                    stop.set()
                    raise RuntimeError('Worker failed: '+setting+' q'+qid)
            except subprocess.TimeoutExpired:
                stop.set()
                directory.mkdir(parents=True,exist_ok=True)
                write(directory/'result.json',dict(setting=setting,question_id=qid,status='timed_out',
                    db_id=question_db[qid],score=0.0,evaluation_status='not_run',
                    usage_complete=False,elapsed_seconds=JOB_DEADLINE))
                return
            result=json.loads((directory/'result.json').read_text(encoding='utf8'))
            if not result['usage_complete']:
                stop.set()
            print(json.dumps({k:result.get(k) for k in ('setting','question_id','score','cloud_api_calls','elapsed_seconds','error_code')}),flush=True)
    lock = args.output/'.running'
    with lock.open('x') as f:
        f.write(str(os.getpid()))
    errors=[]
    try:
        jobs=iter(enumerate(protocol['ids']))
        with ThreadPoolExecutor(max_workers=protocol['planned_concurrency']) as pool:
            pending=set()
            for _ in range(protocol['planned_concurrency']):
                pair=next(jobs,None)
                if pair: pending.add(pool.submit(group,*pair))
            while pending:
                done,pending=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    try: future.result()
                    except Exception as error:
                        errors.append(str(error)); stop.set()
                    if not stop.is_set():
                        pair=next(jobs,None)
                        if pair: pending.add(pool.submit(group,*pair))
    finally:
        lock.unlink()
    data=Path(protocol['data'])
    source=json.loads((data/'source-lock.json').read_text(encoding='utf8'))
    unchanged={db:sha256(data/source['databases'][db]['path'])==expected
               for db,expected in protocol['database_hashes'].items()}
    write(args.output/'completion.json',dict(results=len(read_results(args.output)),
        planned=3*protocol['count_per_setting'],stopped=stop.is_set(),errors=errors,database_unchanged=unchanged))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','run','worker'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--data',type=Path)
    parser.add_argument('--metadata',type=Path)
    parser.add_argument('--count',type=int,default=50)
    parser.add_argument('--seed',type=int,default=20261116)
    parser.add_argument('--concurrency',type=int,choices=range(1,4),default=3)
    parser.add_argument('--setting',choices=SETTINGS)
    parser.add_argument('--qid')
    args=parser.parse_args()
    args.output=args.output.resolve()
    if args.output==CHECKOUT or CHECKOUT in args.output.parents:
        parser.error('Artifacts must be outside source checkout')
    if args.action=='prepare' and (args.data is None or args.metadata is None):
        parser.error('prepare requires --data and --metadata')
    {'prepare':prepare,'run':run,'worker':worker}[args.action](args)


if __name__=='__main__':
    main()
