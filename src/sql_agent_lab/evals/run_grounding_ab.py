"""Run the 24-question development set on a local Ollama model.

Gold rows stay in the scorer. They are not placed in the question or the
grounding block. Results are written one row at a time so a stopped run can resume.
"""
from hashlib import sha256
import argparse
import json
from pathlib import Path
import time

import httpx

from sql_agent_lab.evals.grounding_ab import ARMS, CASES, KNOWLEDGE, build_database, score_sql
from sql_agent_lab.macsql.context import ContextPolicy
from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent
from sql_agent_lab.macsql.reference_core import ROLES, WorkflowConfig
from sql_agent_lab.macsql.run_local import LocalOllamaModel
from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits
from sql_agent_lab.workspace.grounding_context import render_grounding
from sql_agent_lab.workspace.profile import extract_profile
from sql_agent_lab.workspace.retrieval import ground


MODEL_NAME = 'qwen3:4b-instruct'
RUNTIME = {
    'model': MODEL_NAME,
    'temperature': 0,
    'seed': 123,
    'top_p': None,
    'num_ctx': 16384,
    'num_thread': 8,
    'num_gpu': 0,
    'think': False,
    'max_repairs': 1,
    'semantic_review': False,
    'schema_retrieval': True,
    'value_lookup': True,
    'input_budget': 14000,
    'output_reserve': 1000,
    'context_window': 16384,
    'repeats': 1,
}


def case_input(case, profile, arm, strategy='keyword', use_grounding=None):
    """Question text and optional grounding. Expected rows are not included."""
    if 'expected_rows' in json.dumps({'question': case['question']}):
        raise ValueError('Question text must not carry the scorer payload')
    question = Question(case['id'], 'shop', case['question'])
    enabled = ARMS[arm]['use_grounding'] if use_grounding is None else use_grounding
    if not enabled:
        return question, '', {'enabled': False, 'retrieval_called': False, 'retrieval_latency_ms': 0,
                              'strategy': None}
    started = time.perf_counter()
    bundle = ground('shop', profile, KNOWLEDGE, case['question'], cache={}, strategy=strategy)
    block, summary = render_grounding(bundle)
    summary['retrieval_latency_ms'] = round((time.perf_counter() - started) * 1000, 1)
    summary['retrieval_called'] = True
    if 'expected_rows' in block or 'Gold' in block:
        raise ValueError('Grounding block contains scorer material')
    return question, block, summary


def execute_case(database, profile, case, arm, client, strategy='keyword', use_grounding=None):
    question, block, grounding = case_input(
        case, profile, arm, strategy, use_grounding=use_grounding)
    events = []
    tools = SQLiteTools({'shop': database}, ToolLimits(timeout_seconds=30))
    model = LocalOllamaModel(client, MODEL_NAME, RUNTIME['num_ctx'], num_threads=RUNTIME['num_thread'], cpu_only=True)
    agent = ReferenceMacSQLAgent(
        {role: MODEL_NAME for role in ROLES},
        config=WorkflowConfig(
            tools, model, context=ContextPolicy(
                input_budget=RUNTIME['input_budget'], output_reserve=RUNTIME['output_reserve'],
                context_window=RUNTIME['context_window']),
            trace=events.append, schema_retrieval=RUNTIME['schema_retrieval'],
            value_lookup=RUNTIME['value_lookup'], semantic_review=False,
            max_repairs=RUNTIME['max_repairs']))
    agent.grounding_block = block
    started = time.perf_counter()
    error = None
    sql = None
    try:
        sql = agent.run(question)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    grade = score_sql(database, sql, case.get('expected_rows'), ordered=bool(case.get('ordered'))) if sql else {
        'execution_ok': False, 'accurate': False, 'reason': 'no sql'}
    if case.get('expected_rows') is None and grade['execution_ok']:
        grade['accurate'] = None
        grade['reason'] = 'no_expected_result'
    return _record(case, arm, sql, grade, grounding, events, elapsed_ms, error, model.metadata), events, grounding


def _record(case, arm, sql, grade, grounding, events, elapsed_ms, error, metadata):
    completed = next((item for item in reversed(events) if item.get('event') == 'workflow_completed'), {})
    selected = next((item for item in events if item.get('event') == 'schema_selected'), {})
    calls = [item for item in events if item.get('event') == 'call_completed']
    failed_calls = [item for item in events if item.get('event') == 'call_failed']
    usage_complete = not failed_calls and calls and all(
        isinstance(item.get('input_tokens'), int) and isinstance(item.get('output_tokens'), int) for item in calls)
    roles = [item.get('role') for item in events if item.get('event') == 'call_started']
    accurate = grade.get('accurate')
    if error:
        category = 'model_error'
    elif not sql:
        category = 'no_sql'
    elif not grade.get('execution_ok'):
        category = 'execution_error'
    elif accurate is True:
        category = 'correct'
    elif accurate is False:
        category = 'wrong_result'
    else:
        category = 'unscored'
    return {
        'question_id': case['id'],
        'datasource_id': 'shop',
        'arm': arm,
        'model_id': MODEL_NAME,
        'model_digest': metadata.get('digest'),
        'grounding_enabled': bool(grounding.get('enabled')),
        'generated_sql': sql,
        'execution_correct': accurate,
        'execution_ok': grade.get('execution_ok'),
        'execution_error': None if grade.get('execution_ok') else grade.get('reason'),
        'scored': case.get('expected_rows') is not None,
        'selected_tables': sorted((selected.get('selected_schema') or {}).keys()),
        'selector_invoked': selected.get('selector_invoked'),
        'matched_tables': grounding.get('matched_tables') or [],
        'matched_knowledge': [
            {'id': item.get('id'), 'version': item.get('version'), 'content_hash': item.get('content_hash')}
            for item in grounding.get('knowledge') or []],
        'retrieval_called': bool(grounding.get('retrieval_called')),
        'retrieval_hit': set(case['answer_tables']) <= set(grounding.get('matched_tables') or []),
        'agent_call_count': len(roles),
        'roles': roles,
        'retry_count': completed.get('repair_count', 0),
        'refiner_called': 'refiner' in roles,
        'repair_success': bool(completed.get('repair_count', 0) and accurate is True),
        'total_latency_ms': round(elapsed_ms + (grounding.get('retrieval_latency_ms') or 0), 1),
        'agent_latency_ms': elapsed_ms,
        'retrieval_latency_ms': grounding.get('retrieval_latency_ms') or 0,
        'input_tokens': sum(item['input_tokens'] for item in calls) if usage_complete else None,
        'output_tokens': sum(item['output_tokens'] for item in calls) if usage_complete else None,
        'estimated_cost': None,
        'cost_status': 'not_recorded',
        'failure_category': category,
        'error': error,
        'simulated': False,
    }


def summarize(rows):
    """Paired metrics. Unscored questions stay out of the accuracy denominator."""
    def arm_rows(arm, scored_only=False):
        found = [row for row in rows if row['arm'] == arm]
        return [row for row in found if row['scored']] if scored_only else found

    def rate(found, predicate):
        return None if not found else sum(1 for row in found if predicate(row)) / len(found)

    baseline = {row['question_id']: row for row in arm_rows('baseline', True)}
    grounded = {row['question_id']: row for row in arm_rows('grounded', True)}
    shared = sorted(set(baseline) & set(grounded))
    gained = [item for item in shared if baseline[item]['execution_correct'] is not True
              and grounded[item]['execution_correct'] is True]
    lost = [item for item in shared if baseline[item]['execution_correct'] is True
            and grounded[item]['execution_correct'] is not True]
    def mean(found, key):
        values = [row[key] for row in found if isinstance(row.get(key), (int, float))]
        return None if not values else sum(values) / len(values)
    return {
        'rows': len(rows),
        'scored_questions': len(shared),
        'baseline_execution_accuracy': rate(arm_rows('baseline', True), lambda row: row['execution_correct'] is True),
        'grounded_execution_accuracy': rate(arm_rows('grounded', True), lambda row: row['execution_correct'] is True),
        'baseline_execution_success': rate(arm_rows('baseline'), lambda row: row['execution_ok'] is True),
        'grounded_execution_success': rate(arm_rows('grounded'), lambda row: row['execution_ok'] is True),
        'wrong_to_correct': gained,
        'correct_to_wrong': lost,
        'baseline_mean_latency_ms': mean(arm_rows('baseline'), 'total_latency_ms'),
        'grounded_mean_latency_ms': mean(arm_rows('grounded'), 'total_latency_ms'),
        'baseline_input_tokens': mean(arm_rows('baseline'), 'input_tokens'),
        'grounded_input_tokens': mean(arm_rows('grounded'), 'input_tokens'),
        'baseline_output_tokens': mean(arm_rows('baseline'), 'output_tokens'),
        'grounded_output_tokens': mean(arm_rows('grounded'), 'output_tokens'),
        'baseline_refiner_calls': sum(1 for row in arm_rows('baseline') if row['refiner_called']),
        'grounded_refiner_calls': sum(1 for row in arm_rows('grounded') if row['refiner_called']),
        'baseline_repair_success': sum(1 for row in arm_rows('baseline') if row['repair_success']),
        'grounded_repair_success': sum(1 for row in arm_rows('grounded') if row['repair_success']),
        'selector_invocations': sum(1 for row in rows if row.get('selector_invoked')),
        'estimated_cost': None,
    }


def run(output_dir, *, cases=None):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    database = output / 'shop.sqlite'
    if not database.exists():
        build_database(database)
    snapshot = sha256(database.read_bytes()).hexdigest()
    profile = extract_profile(database)
    selected = CASES if not cases else [item for item in CASES if item['id'] in set(cases)]
    journal = output / 'results.jsonl'
    done = set()
    if journal.exists():
        for line in journal.read_text(encoding='utf-8').splitlines():
            if line.strip():
                row = json.loads(line)
                done.add((row['question_id'], row['arm']))
    config = dict(RUNTIME)
    config['database_sha256'] = snapshot
    config['profile_fingerprint'] = profile['fingerprint']
    with httpx.Client(base_url='http://127.0.0.1:11434', trust_env=False, timeout=300.0) as client:
        probe = LocalOllamaModel(client, MODEL_NAME, RUNTIME['num_ctx'], num_threads=8, cpu_only=True)
        config['model_digest'] = probe.metadata.get('digest')
        config['parameter_size'] = probe.metadata.get('details', {}).get('parameter_size')
        config['quantization'] = probe.metadata.get('details', {}).get('quantization_level')
        (output / 'config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
        for case in selected:
            for arm in ('baseline', 'grounded'):
                if (case['id'], arm) in done:
                    continue
                print(f"RUN {case['id']} {arm}", flush=True)
                row, events, grounding = execute_case(database, profile, case, arm, client)
                row['database_sha256'] = snapshot
                trace_dir = output / 'traces'
                trace_dir.mkdir(exist_ok=True)
                (trace_dir / f"{case['id']}-{arm}.jsonl").write_text(
                    ''.join(json.dumps(event, ensure_ascii=False, default=str) + '\n' for event in events),
                    encoding='utf-8')
                if arm == 'grounded':
                    evidence = output / 'grounding'
                    evidence.mkdir(exist_ok=True)
                    (evidence / f"{case['id']}.json").write_text(
                        json.dumps(grounding, ensure_ascii=False, indent=2), encoding='utf-8')
                with journal.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                print(f"DONE {case['id']} {arm} {row['failure_category']} {row['total_latency_ms']}ms", flush=True)
    rows = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines() if line.strip()]
    summary = summarize(rows)
    (output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--cases', nargs='*', default=[])
    args = parser.parse_args(argv)
    summary = run(args.output_dir, cases=args.cases or None)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
