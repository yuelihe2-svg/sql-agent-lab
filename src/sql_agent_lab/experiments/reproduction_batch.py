"""Frozen D-arm experiment on the previous paired sample. Explicit run is paid.

Preparation reads existing author-sourced metadata and cached schema artifacts;
it never calls a model. Reference answers enter evaluation only after generation.
"""
import argparse
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .macsql_reproduction import OriginalMacSQLAgent, ReproductionDeepSeekModel
from .macsql_schema import BirdSchema
from .macsql_upstream import REVISION, load_prompts
from .role_comparison import CHECKOUT, source_hashes, write
from sql_agent_lab.macsql.dataset import Question, load_bird_jsonl
from sql_agent_lab.macsql.deepseek_api import Ledger, MODEL, read_events
from sql_agent_lab.macsql.evaluation_audit import EvaluationSession, SCORER
from sql_agent_lab.macsql.fetch_bird import sha256
from sql_agent_lab.macsql.reference_core import JsonlTrace
from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits

QUESTION_CAP = 550_000
MAX_CALLS = 5
DEADLINE = 900


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def validate_metadata(entry, actual):
    """Check identifiers and report unenforced schema annotations, without edits."""
    names, columns = entry['table_names_original'], entry['column_names_original']
    if len(names) != len(set(names)) or len(columns) != len(entry['column_names']):
        raise ValueError('Malformed metadata identifiers')
    if not set(names) <= set(actual):
        raise ValueError('Missing metadata table in SQLite')
    for i, name in enumerate(names):
        expected = [c for t, c in columns if t == i]
        if len(expected) != len(set(expected)) or not set(expected) <= set(actual[name]['columns']):
            raise ValueError('Missing or duplicate metadata column')

    def identifier(index):
        table, column = columns[index]
        if table < 0 or column not in actual[names[table]]['columns']:
            raise ValueError('Invalid key index')
        return names[table], column

    pks = {identifier(i) for key in entry['primary_keys'] for i in (key if isinstance(key, list) else [key])}
    fks = {(*identifier(a), *identifier(b)) for a, b in entry['foreign_keys']}
    actual_pks = {(t, c) for t in names for c, d in actual[t]['columns'].items() if d['pk']}
    actual_fks = {(t, f['column'], f['table'], f['target']) for t in names for f in actual[t]['foreign_keys']}
    return dict(tables=len(names), columns=sum(t >= 0 for t, _ in columns),
                extra_sqlite_tables=sorted(set(actual)-set(names)),
                extra_sqlite_columns={name: sorted(set(actual[name]['columns'])-
                    {c for t, c in columns if t == i}) for i, name in enumerate(names)
                    if set(actual[name]['columns'])-{c for t, c in columns if t == i}},
                primary_key_annotations=len(pks), foreign_key_annotations=len(fks),
                annotated_primary_keys_not_declared=sorted(pks-actual_pks),
                declared_primary_keys_not_annotated=sorted(actual_pks-pks),
                annotated_foreign_keys_not_declared=sorted(fks-actual_fks),
                declared_foreign_keys_not_annotated=sorted(actual_fks-fks))


def verify_files(root, protocol, *, databases=False):
    if source_hashes() != protocol['source_hashes']:
        raise ValueError('Source changed after freeze')
    for relative, expected in protocol['artifact_hashes'].items():
        if sha256(root/relative) != expected:
            raise ValueError('Frozen artifact changed: '+relative)
    data = Path(protocol['data'])
    if sha256(data/'mini_dev_sqlite.jsonl') != protocol['data_sha256']:
        raise ValueError('Question/reference annotations changed')
    for db, path in protocol['databases'].items():
        stamp = Path(path).stat()
        if [stamp.st_size, stamp.st_mtime_ns] != protocol['database_stamps'][db]:
            raise ValueError('Database file changed')
        if databases and sha256(Path(path)) != protocol['database_hashes'][db]:
            raise ValueError('Database checksum changed')
    load_prompts(protocol['upstream_dir'])


def prepare(args):
    root, previous = args.output, args.previous.resolve()
    if (root/'protocol.json').exists() or (root/'D').exists():
        raise ValueError('Existing protocol/results must not be overwritten')
    old = read(previous/'protocol.json')
    source = read(Path(old['data'])/'source-lock.json')
    questions = read(previous/'questions.json')
    if len(questions) != 50 or [str(q['question_id']) for q in questions] != old['ids']:
        raise ValueError('Previous frozen sample is not the expected 50 questions')
    # Fresh Question values must equal the previous sample; no re-sampling.
    databases = {db: str(Path(old['data'])/source['databases'][db]['path']) for db in old['database_hashes']}
    loaded = {q.question_id: q for q, _ in load_bird_jsonl(
        Path(old['data'])/'mini_dev_sqlite.jsonl', databases, question_ids=old['ids'])}
    for item in questions:
        q = loaded[str(item['question_id'])]
        if any(item.get(key, '') != value for key, value in asdict(q).items() if key != 'question_id'):
            raise ValueError('Previous question text/evidence changed')
    tables = read(root/'metadata/macsql-dev_tables.json')
    entries = {e['db_id']: e for e in tables}
    validation, stamps, schema_hashes = {}, {}, {}
    preparation = read(root/'metadata/preparation-final.json')
    for db, filename in databases.items():
        path = Path(filename)
        if sha256(path) != old['database_hashes'][db]:
            raise ValueError('Database differs from previous experiment')
        stats = path.stat(); stamps[db] = [stats.st_size, stats.st_mtime_ns]
        raw = SQLiteTools({db: path})
        validation[db] = validate_metadata(entries[db], raw.load_schema(db))
        cached = read(root/'schemas'/f'{db}.json')
        if cached['db_id'] != db or preparation[db]['status'] != 'prepared':
            raise ValueError('Schema preparation incomplete')
        expected = {name: [c for t, c in entries[db]['column_names_original'] if t == i]
                    for i, name in enumerate(entries[db]['table_names_original'])}
        if expected != {t: [c['name'] for c in d['columns']] for t, d in cached['tables'].items()}:
            raise ValueError('Prepared schema does not match author metadata')
        schema_hashes[f'schemas/{db}.json'] = sha256(root/'schemas'/f'{db}.json')
    prompts = load_prompts(args.upstream_dir)
    overlap = [str(q['question_id']) for q in questions if any(
        q['question'].strip() in prompt for prompt in prompts.values())]
    if overlap:
        raise ValueError('Frozen question appears verbatim in upstream few-shot examples')
    initial_bytes = {}
    for item in questions:
        q = loaded[str(item['question_id'])]
        schema = BirdSchema(**read(root/'schemas'/f'{q.db_id}.json'))
        desc, fk, _ = schema.render()
        fields = dict(db_id=q.db_id, query=q.question, evidence=q.evidence, desc_str=desc, fk_str=fk)
        initial_bytes[q.question_id] = {key: len(prompts[key].format(**fields).encode('utf8'))
            for key in ('selector_template', 'decompose_template_bird')}
    if max(v for item in initial_bytes.values() for v in item.values()) > 100_000:
        raise ValueError('Unmodified initial prompt exceeds reproduction limit')
    write(root/'questions.json', questions)
    write(root/'metadata/validation.json', validation)
    write(root/'metadata/prompt-preflight.json', dict(exact_fewshot_overlap=overlap, initial_bytes=initial_bytes))
    baseline = {str(p.relative_to(previous)): sha256(p) for p in previous.glob('[ABC]/q*/result.json')}
    if len(baseline) != 150:
        raise ValueError('Missing historical comparison results')
    artifact_files = ['questions.json', 'metadata/macsql-dev_tables.json', 'metadata/source-manifest.json',
                      'metadata/validation.json', 'metadata/preparation-final.json', 'metadata/prompt-preflight.json']
    protocol = dict(created_utc=datetime.now(timezone.utc).isoformat(),
        git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=CHECKOUT,text=True).strip(),
        source_hashes=source_hashes(), previous=str(previous), previous_protocol_sha256=sha256(previous/'protocol.json'),
        baseline_result_hashes=baseline, upstream_revision=REVISION, upstream_dir=str(args.upstream_dir.resolve()),
        artifact_hashes=schema_hashes | {p:sha256(root/p) for p in artifact_files},
        data=old['data'],data_sha256=old['data_sha256'],databases=databases,
        database_hashes=old['database_hashes'],database_stamps=stamps,ids=old['ids'],
        model=MODEL,documented_version=old['documented_version'],temperature=0.1,thinking=False,
        max_output_tokens=3200,max_prompt_bytes=100_000, max_calls=MAX_CALLS,
        question_token_cap=QUESTION_CAP,total_token_cap=50*QUESTION_CAP,
        max_output_tokens_batch=50*MAX_CALLS*3200,
        conservative_price_upper_usd=(50*MAX_CALLS*(100_000+544)*.30+50*MAX_CALLS*3200*1.20)/1e6,
        pricing_source='https://api-docs.deepseek.com/quick_start/pricing/',
        scorer=SCORER,evaluation_timeout=30,execution_timeout=120,job_deadline=DEADLINE,
        repetitions=1,automatic_retries=0,planned_concurrency=3,
        reference_answers_in_model_inputs=False,semantic_review=False,
        schema_preparation='cached once per database; original frequency-ranked values; timings separate',
        comparison='historical paired same 50; configuration bundle comparison, not single-factor ablation')
    write(root/'protocol.json', protocol)
    verify_files(root, protocol)
    print(json.dumps(dict(prepared=50,databases=len(databases),exact_fewshot_overlap=overlap,
                         max_initial_prompt_bytes=max(v for item in initial_bytes.values() for v in item.values()))))


def worker(args):
    p = read(args.output/'protocol.json')
    verify_files(args.output, p)
    if args.qid not in p['ids']:
        raise ValueError('Question outside frozen sample')
    target = args.output/'D'/('q'+args.qid)
    target.mkdir(parents=True, exist_ok=False)
    item = next(q for q in read(args.output/'questions.json') if str(q['question_id']) == args.qid)
    question = Question(args.qid, item['db_id'], item['question'], item.get('evidence', ''))
    schema = BirdSchema(**read(args.output/'schemas'/f'{question.db_id}.json'))
    trace = JsonlTrace(target/'trace.jsonl')
    ledger = Ledger(target/'api-calls.jsonl', batch_cap=QUESTION_CAP, question_cap=QUESTION_CAP, max_calls=MAX_CALLS)
    write(target/'manifest.json', dict(setting='D',question=asdict(question),simulated=False,
          reference_answers_in_model_inputs=False,protocol='../../protocol.json'))
    result = dict(setting='D',question_id=args.qid,db_id=question.db_id,status='failed',score=0.0,
                  evaluation_status='not_run',sql=None,reference_answers_in_model_inputs=False)
    start = time.monotonic()
    try:
        tools = SQLiteTools({question.db_id:p['databases'][question.db_id]},ToolLimits(timeout_seconds=120))
        model = ReproductionDeepSeekModel(os.environ.get('DEEPSEEK_API_KEY'), ledger, args.qid)
        agent = OriginalMacSQLAgent(tools=tools,model=model,model_name=MODEL,schema=schema,
                                   upstream_dir=p['upstream_dir'],trace=trace)
        outcome = agent.run(question)
        result.update(sql=outcome.sql,agent_seconds=time.monotonic()-start,outcome=asdict(outcome),
                      selector_invoked=outcome.selector_invoked,repair_count=outcome.repairs,
                      final_sql_checked=outcome.final_sql_checked,stop_reason=outcome.stop_reason)
        # Same independent scorer and limits as A/B/C, after agent termination.
        evaluation_tools = SQLiteTools(tools.databases, ToolLimits(timeout_seconds=p['evaluation_timeout']))
        _, reference = load_bird_jsonl(Path(p['data'])/'mini_dev_sqlite.jsonl', p['databases'],
                                       question_ids=[args.qid])[0]
        evaluator = EvaluationSession(evaluation_tools,reference,p['database_hashes'][question.db_id],
                                      artifact=target/'evaluation-evidence.json')
        scored = evaluator.score(outcome.sql,'final')
        result.update(score=scored['score'],evaluation_status=scored['status'],evaluation_reason=scored['reason'],
                      evaluation_seconds=scored['elapsed_seconds'],
                      status='completed' if scored.get('candidate',{}).get('complete') else 'failed',
                      feedback={'status':'ok' if scored.get('candidate',{}).get('complete') else 'error'})
    except Exception as error:
        result.update(error_type=type(error).__name__,error_code=getattr(error,'code',type(error).__name__))
        if result['sql'] is not None:
            result.update(score=None,evaluation_status='error')
    calls = read_events(target/'api-calls.jsonl')
    starts = [e for e in calls if e['event']=='request_started']
    ends = [e for e in calls if e['event']=='request_finished']
    result.update(elapsed_seconds=time.monotonic()-start,agent_seconds=result.get('agent_seconds',time.monotonic()-start),
                  cloud_api_calls=len(starts),usage_complete=not ledger.totals()[2],
                  input_tokens=sum(e.get('input_tokens',0) for e in ends),
                  output_tokens=sum(e.get('output_tokens',0) for e in ends),
                  role_calls=dict(Counter(e['role'] for e in starts)),
                  estimated_cost_usd_peak_no_cache=sum(e.get('estimated_cost_usd_peak_no_cache',0) for e in ends),
                  returned_models=sorted({e.get('returned_model','unknown') for e in ends}))
    write(target/'result.json',result)


def run(args):
    p = read(args.output/'protocol.json')
    verify_files(args.output,p,databases=True)
    if not os.environ.get('DEEPSEEK_API_KEY'):
        raise ValueError('DeepSeek credential unavailable; value is never logged')
    # Never silently rerun or resume a partially paid arm.
    if (args.output/'D').exists():
        raise ValueError('D arm already exists; inspect saved jobs before any further paid calls')
    started = time.monotonic()
    stop, errors = False, []
    def launch(qid):
        with (args.output/('D-q'+qid+'.worker.log')).open('w',encoding='utf8') as log:
            process = subprocess.run([sys.executable,'-B','-m','sql_agent_lab.experiments.reproduction_batch','worker','--output',str(args.output),
                                      '--qid',qid],cwd=CHECKOUT,stdout=log,stderr=subprocess.STDOUT,
                                     timeout=DEADLINE,env={**os.environ,'LANGCHAIN_TRACING_V2':'false','LANGSMITH_TRACING':'false'})
        if process.returncode:
            raise RuntimeError('Worker failed for q'+qid)
        return read(args.output/'D'/('q'+qid)/'result.json')
    with (args.output/'.running').open('x') as guard:
        guard.write(str(os.getpid()))
    try:
        jobs = iter(p['ids'])
        with ThreadPoolExecutor(max_workers=p['planned_concurrency']) as pool:
            pending = {pool.submit(launch,next(jobs)) for _ in range(p['planned_concurrency'])}
            while pending:
                done,pending = wait(pending,return_when=FIRST_COMPLETED)
                for task in done:
                    try:
                        result = task.result()
                        print(json.dumps({k:result.get(k) for k in ('question_id','score','cloud_api_calls','repair_count','error_code')}),flush=True)
                        if not result['usage_complete']:
                            stop = True
                    except Exception as error:
                        errors.append(type(error).__name__);stop = True
                while not stop and len(pending)<p['planned_concurrency']:
                    qid = next(jobs,None)
                    if qid is None:break
                    pending.add(pool.submit(launch,qid))
    finally:
        (args.output/'.running').unlink()
    unchanged = {db:sha256(Path(filename))==p['database_hashes'][db] for db,filename in p['databases'].items()}
    write(args.output/'completion.json',dict(results=len(list((args.output/'D').glob('q*/result.json'))),
          planned=50,stopped=stop,errors=errors,database_unchanged=unchanged,wall_seconds=time.monotonic()-started))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','run','worker'])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--previous',type=Path)
    parser.add_argument('--upstream-dir',type=Path)
    parser.add_argument('--qid')
    args = parser.parse_args(); args.output=args.output.resolve()
    if args.output==CHECKOUT or CHECKOUT in args.output.parents:
        parser.error('Artifacts must stay outside the checkout')
    if args.action=='prepare' and (not args.previous or not args.upstream_dir):
        parser.error('prepare requires --previous and --upstream-dir')
    {'prepare':prepare,'run':run,'worker':worker}[args.action](args)


if __name__=='__main__':
    main()
