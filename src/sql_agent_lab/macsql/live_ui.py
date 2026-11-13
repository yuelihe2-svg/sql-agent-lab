"""Local workflow observatory. Run with --data-dir and --output-dir; opens no cloud API."""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlparse, parse_qs
from uuid import uuid4

import httpx
from .context import ContextPolicy
from .dataset import Question, load_bird_jsonl, score_sql
from .fetch_bird import sha256
from .metadata import load_metadata
from .reference_baseline import ReferenceMacSQLAgent
from .reference_core import JsonlTrace, ROLES, WorkflowConfig
from .run_local import LocalOllamaModel
from .tools import SQLiteTools, ToolLimits
from .evaluation_audit import EvaluationSession
from .trace_support import TraceEmitter, review_timing
from .semantic_review import CONTRACT


class Stopped(RuntimeError):
    pass


class ObservedTools:
    """Observe public workflow tool calls; internal helper calls are not double-counted."""
    names = {'load_schema', 'search_schema', 'lookup_values',
             'compile_sql_readonly', 'execute_sql_readonly', 'diagnose_sql_readonly'}

    def __init__(self, tools, emit, stop):
        self.tools, self.emit, self.stop = tools, emit, stop

    def __getattr__(self, name):
        method = getattr(self.tools, name)
        if name not in self.names:
            return method
        def call(*args, **kwargs):
            if self.stop.is_set():
                raise Stopped('Stopped before the next tool call')
            call_id = uuid4().hex
            inputs = dict(inspect.signature(method).bind(*args, **kwargs).arguments)
            if 'schema' in inputs:
                inputs['schema'] = '[previously loaded schema]'
            self.emit({'event': 'tool_started', 'tool': name, 'tool_call_id': call_id, 'input': inputs})
            started = time.monotonic()
            try:
                output = method(*args, **kwargs)
            except Exception as error:
                self.emit({'event': 'tool_failed', 'tool': name, 'tool_call_id': call_id,
                           'error': str(error), 'elapsed_seconds': time.monotonic() - started})
                raise
            self.emit({'event': 'tool_completed', 'tool': name, 'tool_call_id': call_id,
                       'output': output, 'elapsed_seconds': time.monotonic() - started})
            return output
        return call


class Observatory:
    def __init__(self, data, output, metadata=None, history=None, analysis_dir=None, recorded_batches=None):
        self.data, self.output = Path(data).resolve(), Path(output).resolve()
        checkout = Path(__file__).resolve().parents[3]
        if self.output == checkout or checkout in self.output.parents:
            raise ValueError('Keep run outputs outside the checkout')
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock_data = json.loads((self.data / 'source-lock.json').read_text())
        if sha256(self.data / 'mini_dev_sqlite.jsonl') != self.lock_data['annotations_sha256']:
            raise ValueError('Dataset checksum mismatch')
        self.databases = {db: self.data / entry['path'] for db, entry in self.lock_data['databases'].items()}
        self.dataset = load_bird_jsonl(self.data / 'mini_dev_sqlite.jsonl', self.databases)
        self.metadata = load_metadata(metadata) if metadata else None
        self.history = Path(history).resolve() if history else None
        self.analysis_dir = Path(analysis_dir).resolve() if analysis_dir else None
        self.recorded_batches = [Path(p).resolve(strict=True) for p in recorded_batches or []]
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.active = None

    def paths(self):
        paths = {p.name: p for p in self.output.iterdir() if p.is_dir() and (p/'manifest.json').exists()}
        if self.history:
            for p in self.history.glob('local-pilot-*'):
                if (p/'manifest.json').exists():
                    paths['history-' + p.name] = p
        paths.update({key: value['path'] for key, value in self.recorded_runs().items()})
        return paths

    def recorded_runs(self):
        """Explicitly registered local records; never run SQL or a model while viewing."""
        records = {}
        for index, root in enumerate(getattr(self, 'recorded_batches', [])):
            protocol = json.loads((root/'protocol.json').read_text(encoding='utf8'))
            for qid in protocol['ids']:
                if not str(qid).isdecimal():
                    raise ValueError('Invalid recorded question ID')
                path = (root/('q' + str(qid))).resolve()
                if path.parent != root:
                    raise ValueError('Recorded run must stay inside its registered directory')
                if (path/'manifest.json').is_file():
                    records[f'recorded-{index}-q{qid}'] = {
                        'path': path, 'batch_id': f'recorded-{index}', 'protocol': protocol}
        return records

    def run_index(self):
        recorded = self.recorded_runs()
        rows = []
        for key, path in sorted(self.paths().items(), reverse=True):
            manifest = json.loads((path/'manifest.json').read_text(encoding='utf8'))
            result = json.loads((path/'result.json').read_text(encoding='utf8')) if (path/'result.json').exists() else {}
            rows.append({'id': key, 'question': manifest['question'], 'model': manifest.get('model', {}).get('name'),
                         'recorded': key in recorded, 'batch_id': recorded.get(key, {}).get('batch_id'),
                         **{k: result.get(k) for k in ('score', 'status', 'semantic_status', 'elapsed_seconds',
                             'cloud_api_calls', 'input_tokens', 'output_tokens', 'estimated_cost_usd_peak_no_cache')}})
        batches = []
        for index, root in enumerate(getattr(self, 'recorded_batches', [])):
            protocol = json.loads((root/'protocol.json').read_text(encoding='utf8'))
            items = [r for r in rows if r['batch_id'] == f'recorded-{index}']
            scored = [r for r in items if r['score'] in (0, 1)]
            correct = sum(r['score'] == 1 for r in scored)
            totals = {k: sum(r[k] for r in items) if items and all(r[k] is not None for r in items) else None
                      for k in ('cloud_api_calls','input_tokens','output_tokens','elapsed_seconds','estimated_cost_usd_peak_no_cache')}
            batches.append({'id': f'recorded-{index}', 'name': protocol.get('documented_version', protocol['model']),
                            'planned': len(protocol['ids']), 'recorded': len(items), 'scored': len(scored),
                            'correct': correct, 'accuracy': correct/len(scored) if scored else None,
                            'rows': sorted(items, key=lambda r: protocol['ids'].index(str(r['question']['question_id']))),
                            'totals': totals})
        return {'active': self.active, 'runs': rows, 'batches': batches}

    def read_run(self, run_id):
        path = self.paths().get(run_id)
        if path is None:
            raise ValueError('Unknown run')
        events = []
        if (path/'trace.jsonl').exists():
            for line in (path/'trace.jsonl').read_text(encoding='utf8').splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # A concurrent append may not have written its final newline yet.
        result = json.loads((path/'result.json').read_text(encoding='utf8')) if (path/'result.json').exists() else None
        evaluation = (json.loads((path/'evaluation-evidence.json').read_text(encoding='utf8'))
                      if result is not None and (path/'evaluation-evidence.json').exists() else None)
        recorded = self.recorded_runs().get(run_id)
        # Normalize the display timestamp without changing the saved trace.
        events = [{**e, 'elapsed': e['elapsed_seconds_since_start']}
                  if 'elapsed' not in e and 'elapsed_seconds_since_start' in e else e for e in events]
        return {'id': run_id, 'historical': run_id.startswith('history-') or recorded is not None,
                'recorded': recorded is not None,
                'recorded_model_version': recorded['protocol'].get('documented_version') if recorded else None,
                'manifest': json.loads((path/'manifest.json').read_text(encoding='utf8')),
                'events': events, 'result': result, 'evaluation_evidence': evaluation, 'active': self.active == run_id,
                'stop_requested': self.active == run_id and self.stop.is_set()}

    def analysis_view(self):
        """Annotate display states from existing results without changing history."""
        data = json.loads((self.analysis_dir/'analysis.json').read_text(encoding='utf8'))
        allowed = {str((p/'result.json').resolve()):p for p in self.paths().values()}
        for row in data['rows']:
            source = str(Path(row.get('source','')).resolve())
            if source not in allowed or not Path(source).exists():
                continue
            result = json.loads(Path(source).read_text(encoding='utf8'))
            row['semantic_status'] = result.get('semantic_status')
            trace_path = allowed[source] / 'trace.jsonl'
            events = [json.loads(line) for line in trace_path.read_text(encoding='utf8').splitlines()] if trace_path.exists() else []
            row['review_timing'] = review_timing(result, events)
        return data

    def start(self, payload):
        if payload.get('question_id') is not None:
            pair = next((p for p in self.dataset if p[0].question_id == str(payload['question_id'])), None)
            if pair is None:
                raise ValueError('Unknown benchmark question')
            question, reference = pair
        else:
            db, text = payload.get('db_id'), payload.get('question', '')
            if db not in self.databases or not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
                raise ValueError('Choose a database and enter a question (maximum 4000 characters)')
            question, reference = Question('custom', db, text.strip()), None
        with self.lock:
            if self.active:
                raise ValueError('A run is already active')
            run_id = datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:6]
            path = self.output / run_id
            path.mkdir()
            manifest = {'question': asdict(question), 'model': {'name': 'qwen3:4b-instruct'},
                        'mode': 'local_inference', 'created_at': datetime.now(timezone.utc).isoformat(),
                        'benchmark': reference is not None, 'schema_retrieval': bool(payload.get('retrieval', True)),
                        'semantic_review': bool(payload.get('semantic_review', False)),
                        'semantic_review_contract': CONTRACT if payload.get('semantic_review') else 'disabled',
                        'workflow_mode': 'optional_semantic_review' if payload.get('semantic_review') else 'execution_repair_only',
                        'max_repairs': 1,
                        'semantic_output_tokens': 1600,
                        'cloud_api_calls': 0}
            (path/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
            self.active = run_id
            self.stop.clear()
            threading.Thread(target=self.work, args=(run_id, question, reference, manifest), daemon=True).start()
        return run_id

    def work(self, run_id, question, reference, manifest):
        path = self.output / run_id
        trace = JsonlTrace(path/'trace.jsonl')
        started = time.monotonic()
        writer = TraceEmitter(trace, run_id, question.question_id, started=started)
        def emit(event):
            if event['event'] == 'call_started' and self.stop.is_set():
                raise Stopped('Stopped before the next model call')
            writer(event)
        result = {'status': 'failed', 'score': None, 'cloud_api_calls': 0}
        try:
            emit({'event': 'run_preparing'})
            db = self.databases[question.db_id]
            before = sha256(db)
            if before != self.lock_data['databases'][question.db_id]['sha256']:
                raise ValueError('Database checksum mismatch')
            raw = SQLiteTools({question.db_id: db}, ToolLimits(timeout_seconds=30), metadata=self.metadata)
            observed = ObservedTools(raw, emit, self.stop)
            with httpx.Client(base_url='http://127.0.0.1:11434', trust_env=False,
                              follow_redirects=False, timeout=600) as client:
                model = LocalOllamaModel(client, 'qwen3:4b-instruct', 16384, num_threads=8, cpu_only=True)
                emit({'event': 'model_ready', 'model': model.metadata})
                config = WorkflowConfig(observed, model, context=ContextPolicy(input_budget=14000,
                    context_window=16384), trace=emit, schema_retrieval=manifest['schema_retrieval'],
                    value_lookup=manifest['schema_retrieval'], semantic_review=manifest.get('semantic_review', False))
                sql = ReferenceMacSQLAgent({role: 'qwen3:4b-instruct' for role in ROLES}, config=config).run(question)
                if self.stop.is_set():
                    raise Stopped('Stopped after the scheduled operation completed')
                feedback = raw.execute_sql_readonly(question.db_id, sql)
                result.update(sql=sql, feedback=feedback, status='completed' if feedback['status']=='ok' else 'failed')
                if reference:
                    emit({'event': 'evaluation_started', 'note': 'Reference SQL is accessible only to the evaluator'})
                    evaluator = EvaluationSession(raw,reference,before,artifact=path/'evaluation-evidence.json',emit=emit)
                    evaluation = evaluator.score(sql,'submitted')
                    result.update(score=evaluation['score'],evaluation_status=evaluation['status'],
                                  evaluation_evidence_file='evaluation-evidence.json')
                    if evaluation['status']=='error': result['status']='evaluation_failed'
                    emit({'event':'evaluation_completed','score':evaluation['score'],'status':evaluation['status']})
                result['database_unchanged'] = sha256(db) == before
        except Stopped as error:
            result.update(status='stopped', error=str(error))
        except Exception as error:
            result.update(status='failed', error_type=type(error).__name__, error=str(error))
        finally:
            records = [json.loads(line) for line in (path/'trace.jsonl').read_text(encoding='utf8').splitlines()]
            completed = next((e for e in reversed(records) if e['event']=='workflow_completed'), {})
            result['semantic_status'] = completed.get('semantic_status', 'not_completed')
            result['semantic_metrics'] = completed.get('semantic_metrics', {})
            result['output_columns_report'] = completed.get('output_columns_report', {})
            result['output_requirements'] = completed.get('output_requirements', {})
            result['output_requirements_assessment'] = completed.get('output_requirements_assessment', {})
            result['output_requirements_protocol_warnings'] = completed.get('output_requirements_protocol_warnings', [])
            result['output_projection_suggestion'] = completed.get('output_projection_suggestion', {})
            result['output_requirements_status'] = completed.get('output_requirements_status', 'not_run')
            result['output_type_check'] = completed.get('output_type_check', {'status': 'unverified'})
            if result['status']=='completed' and result['output_type_check']['status']=='mismatch':
                result['status']='needs_review'
            result['semantic_report'] = next((e['report'] for e in reversed(records)
                                              if e['event']=='semantic_review_completed'), None)
            result['semantic_guard'] = next((e for e in reversed(records)
                                             if e['event']=='semantic_revision_guard'), None)
            result['elapsed_seconds'] = time.monotonic() - started
            emit({'event': 'run_finished', **result})
            temporary = path/'result.tmp'
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            temporary.replace(path/'result.json')
            with self.lock:
                self.active = None


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, data, status=200, content_type='application/json'):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type + '; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                return self.send({'error': 'Local host only'}, 403)
            url = urlparse(self.path)
            try:
                if url.path == '/':
                    return self.send(Path(__file__).with_suffix('.html').read_bytes(), content_type='text/html')
                if url.path == '/analysis':
                    return self.send(Path(__file__).with_name('analysis.html').read_bytes(), content_type='text/html')
                if url.path in ('/api/analysis', '/analysis.csv', '/analysis.xlsx'):
                    directory = getattr(app, 'analysis_dir', None)
                    if directory is None:
                        return self.send({'error': '未配置错误分析目录；使用 --analysis-dir 指定本地报告'}, 404)
                    if url.path == '/api/analysis':
                        view = getattr(app,'analysis_view',None)
                        return self.send(view() if view else json.loads((directory/'analysis.json').read_text(encoding='utf8')))
                    names = {'/api/analysis': ('analysis.json', 'application/json'),
                             '/analysis.csv': ('逐题阶段分析.csv', 'text/csv'),
                             '/analysis.xlsx': ('outputs/stage-audit/错误分析.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')}
                    name, mime = names[url.path]
                    return self.send((directory / name).read_bytes(), content_type=mime)
                if url.path == '/api/catalog':
                    return self.send({'databases': sorted(app.databases), 'questions': [asdict(q) for q,_ in app.dataset],
                                      'semantic_review_contract': CONTRACT, 'semantic_output_tokens': 1600})
                if url.path == '/api/runs':
                    return self.send(app.run_index())
                if url.path == '/api/run':
                    return self.send(app.read_run(parse_qs(url.query).get('id', [''])[0]))
                self.send({'error': 'Not found'}, 404)
            except (ValueError, OSError) as error:
                self.send({'error': str(error)}, 400)

        def do_POST(self):
            # Reject cross-origin browser requests and DNS rebinding hostnames.
            host = self.headers.get('Host', '')
            expected = f'127.0.0.1:{self.server.server_port}'
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 20000:
                    raise ValueError('Request too large')
                # Consume a bounded body before rejecting; unread bytes can cause
                # a connection reset instead of delivering the 403 on Windows.
                body = self.rfile.read(size)
                if host != expected or self.headers.get('Origin', 'http://' + expected) != 'http://' + expected:
                    return self.send({'error': 'Local same-origin requests only'}, 403)
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError('Expected a JSON object')
                if self.path == '/api/start':
                    return self.send({'id': app.start(payload)})
                if self.path == '/api/stop':
                    app.stop.set()
                    return self.send({'status': 'stop_requested'})
                self.send({'error': 'Not found'}, 404)
            except (ValueError, TypeError) as error:
                self.send({'error': str(error)}, 400)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--history', type=Path)
    parser.add_argument('--analysis-dir', type=Path, help='Read-only local historical analysis exports')
    parser.add_argument('--recorded-batch', type=Path, action='append', default=[],
                        help='Read an existing API batch without invoking models or evaluation')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    app = Observatory(args.data_dir, args.output_dir, args.metadata, args.history, args.analysis_dir, args.recorded_batch)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    print(f'AgentOpt Observatory: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.stop.set()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
