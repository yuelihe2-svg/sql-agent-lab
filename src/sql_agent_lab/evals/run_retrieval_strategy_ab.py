"""Paired local-model run for grounding off, keyword, bm25, and hybrid.

Gold rows stay in the scorer. If the local model is not running, the summary
says not_measured and no score is written.
"""
import argparse
import json
from hashlib import sha256
from pathlib import Path

import httpx

from sql_agent_lab.evals.grounding_ab import CASES, build_database
from sql_agent_lab.evals.run_grounding_ab import (
    MODEL_NAME, RUNTIME, execute_case,
)
from sql_agent_lab.macsql.run_local import LocalOllamaModel
from sql_agent_lab.workspace.profile import extract_profile


ARMS = (
    ('off', False, 'keyword'),
    ('keyword', True, 'keyword'),
    ('bm25', True, 'bm25'),
    ('hybrid', True, 'hybrid'),
)


def summarize_strategies(rows):
    def arm_rows(arm, scored_only=False):
        found = [row for row in rows if row['arm'] == arm]
        return [row for row in found if row['scored']] if scored_only else found

    def rate(found, predicate):
        return None if not found else sum(1 for row in found if predicate(row)) / len(found)

    def mean(found, key):
        values = [row[key] for row in found if isinstance(row.get(key), (int, float))]
        return None if not values else sum(values) / len(values)

    by_arm = {arm: {row['question_id']: row for row in arm_rows(arm, True)} for arm, _enabled, _strategy in ARMS}
    off = by_arm['off']
    summary = {
        'rows': len(rows),
        'scored_questions': len(off),
        'estimated_cost': None,
        'model_run': bool(rows),
    }
    for arm, _enabled, _strategy in ARMS:
        found = arm_rows(arm, True)
        summary[arm] = {
            'execution_accuracy': rate(found, lambda row: row['execution_correct'] is True),
            'execution_success': rate(arm_rows(arm), lambda row: row['execution_ok'] is True),
            'mean_latency_ms': mean(arm_rows(arm), 'total_latency_ms'),
            'input_tokens': mean(arm_rows(arm), 'input_tokens'),
            'output_tokens': mean(arm_rows(arm), 'output_tokens'),
            'refiner_calls': sum(1 for row in arm_rows(arm) if row['refiner_called']),
            'repair_success': sum(1 for row in arm_rows(arm) if row['repair_success']),
        }
        if arm == 'off':
            continue
        shared = sorted(set(off) & set(by_arm[arm]))
        summary[arm]['wrong_to_correct'] = [
            item for item in shared
            if off[item]['execution_correct'] is not True and by_arm[arm][item]['execution_correct'] is True]
        summary[arm]['correct_to_wrong'] = [
            item for item in shared
            if off[item]['execution_correct'] is True and by_arm[arm][item]['execution_correct'] is not True]
    return summary


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
    config['arms'] = [arm for arm, _enabled, _strategy in ARMS]
    config['database_sha256'] = snapshot
    config['profile_fingerprint'] = profile['fingerprint']
    try:
        with httpx.Client(base_url='http://127.0.0.1:11434', trust_env=False, timeout=300.0) as client:
            probe = LocalOllamaModel(client, MODEL_NAME, RUNTIME['num_ctx'], num_threads=8, cpu_only=True)
            config['model_digest'] = probe.metadata.get('digest')
            config['parameter_size'] = probe.metadata.get('details', {}).get('parameter_size')
            config['quantization'] = probe.metadata.get('details', {}).get('quantization_level')
            (output / 'config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
            for case in selected:
                for arm, enabled, strategy in ARMS:
                    if (case['id'], arm) in done:
                        continue
                    print(f'RUN {case["id"]} {arm}', flush=True)
                    row, events, grounding = execute_case(
                        database, profile, case, arm, client, strategy=strategy, use_grounding=enabled)
                    row['retrieval_strategy'] = strategy if enabled else None
                    row['database_sha256'] = snapshot
                    trace_dir = output / 'traces'
                    trace_dir.mkdir(exist_ok=True)
                    (trace_dir / f'{case["id"]}-{arm}.jsonl').write_text(
                        ''.join(json.dumps(event, ensure_ascii=False, default=str) + '\n' for event in events),
                        encoding='utf-8')
                    if enabled:
                        evidence = output / 'grounding'
                        evidence.mkdir(exist_ok=True)
                        (evidence / f'{case["id"]}-{arm}.json').write_text(
                            json.dumps(grounding, ensure_ascii=False, indent=2), encoding='utf-8')
                    with journal.open('a', encoding='utf-8') as handle:
                        handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                    print(f'DONE {case["id"]} {arm} {row["failure_category"]} {row["total_latency_ms"]}ms', flush=True)
    except Exception as error:
        if journal.exists() and journal.read_text(encoding='utf-8').strip():
            raise
        summary = {'model_run': False, 'execution_accuracy': 'not_measured', 'reason': f'{type(error).__name__}: {error}'}
        (output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        return summary
    rows = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines() if line.strip()]
    summary = summarize_strategies(rows)
    (output / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--cases', nargs='*', default=[])
    args = parser.parse_args(argv)
    print(json.dumps(run(args.output_dir, cases=args.cases or None), indent=2))


if __name__ == '__main__':
    main()
