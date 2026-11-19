"""Local SQLite observatory with optional BIRD evaluation; startup invokes no models."""
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
from .dataset import Question, score_sql
from .data_sources import SourceRegistry
from .bird_benchmark import BirdBenchmark
from .fetch_bird import sha256
from .metadata import load_metadata
from .reference_baseline import ReferenceMacSQLAgent
from .reference_core import JsonlTrace, ROLES, WorkflowConfig
from .run_local import LocalOllamaModel
from .tools import SQLiteTools, ToolLimits
from .evaluation_audit import EvaluationSession
from .trace_support import TraceEmitter, review_timing
from .semantic_review import CONTRACT
from .task_lifecycle import (change_task, event_stage, new_task, result_task_state,
                             task_view, write_json_atomic)


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
    def __init__(self, data, output, metadata=None, history=None, analysis_dir=None, recorded_batches=None,
                 *, sources=None, workspace_db=None):
        self.data = Path(data).resolve() if data else None
        self.output = Path(output).resolve()
        checkout = Path(__file__).resolve().parents[3]
        if self.output == checkout or checkout in self.output.parents:
            raise ValueError('Keep run outputs outside the checkout')
        self.output.mkdir(parents=True, exist_ok=True)
        self.sources = SourceRegistry()
        if sources:
            self.sources.load(sources)
        self.benchmark = BirdBenchmark(self.data, self.sources) if self.data else None
        self.databases = self.sources.databases
        if not self.databases:
            raise ValueError('Configure at least one SQLite source (--sources) or BIRD dataset (--data-dir)')
        # SourceRegistry remains the authority for SQLite paths. The optional
        # metadata file receives only the public catalog.
        self.workspace_db = None
        if workspace_db is not None:
            from sql_agent_lab.workspace.database import sync_registered_sources
            sync_registered_sources(workspace_db, self.sources.catalog())
            self.workspace_db = Path(workspace_db)
        self.dataset = self.benchmark.dataset if self.benchmark else []
        self.metadata = load_metadata(metadata) if metadata else None
        self.history = Path(history).resolve() if history else None
        self.analysis_dir = Path(analysis_dir).resolve() if analysis_dir else None
        self.recorded_batches = [Path(p).resolve(strict=True) for p in recorded_batches or []]
        self.lock, self.stop = threading.Lock(), threading.Event()
        self.active = None
        self.tasks = {}  # Current-process snapshots, also available if saving fails.
        self._run_conversation = {}

    def catalog(self):
        return {'databases': sorted(self.databases), 'sources': self.sources.catalog(),
                'benchmark_available': self.benchmark is not None,
                'questions': [asdict(q) for q, _ in self.dataset],
                'semantic_review_contract': CONTRACT, 'semantic_output_tokens': 1600}

    def _save_task(self, run_id, task):
        """Called under self.lock so stop requests and worker updates cannot race."""
        task = {key: value for key, value in task.items() if key != 'persistence_error'}
        self.tasks[run_id] = task
        try:
            write_json_atomic(self.output / run_id / 'task.json', task)
        except OSError as error:
            task['persistence_error'] = str(error)
            raise
        else:
            self._project_workspace(run_id)

    def _project_workspace(self, run_id):
        """Copy a saved run into the optional workspace database.

        The run directory is already the source of truth. A database error is
        reported on the in-memory task and does not rewrite that directory.
        """
        if self.workspace_db is None:
            return
        try:
            import sqlite3
            from sql_agent_lab.workspace.database import WorkspaceStore, record_saved_run
            path = self.output / run_id
            manifest = json.loads((path / 'manifest.json').read_text(encoding='utf8'))
            task = json.loads((path / 'task.json').read_text(encoding='utf8'))
            result_path = path / 'result.json'
            result = json.loads(result_path.read_text(encoding='utf8')) if result_path.is_file() else None
            record_saved_run(
                WorkspaceStore(self.workspace_db), run_id=run_id, manifest=manifest, task=task,
                result=result, artifact_dir=str(path.resolve()),
                conversation_id=self._run_conversation.get(run_id))
        except (OSError, sqlite3.Error, ValueError) as error:
            if run_id in self.tasks:
                self.tasks[run_id]['persistence_error'] = str(error)

    def _task_view(self, run_id, path, result):
        return task_view(path, result, active=self.active == run_id,
                         stop_requested=self.active == run_id and self.stop.is_set(),
                         managed=path.resolve().parent == self.output.resolve(),
                         current=getattr(self, 'tasks', {}).get(run_id))

    def request_stop(self, run_id):
        """Target one task: a stale browser must never stop a newer task."""
        if not isinstance(run_id, str) or not run_id:
            raise ValueError('A run ID is required to stop a task')
        with self.lock:
            if run_id != self.active:
                return {'id': run_id, 'status': 'not_active'}
            task = self.tasks[run_id]
            try:
                if task['state'] != 'stopping':
                    self._save_task(run_id, change_task(task, state='stopping'))
            finally:
                # Even a full disk must not prevent cooperative cancellation.
                self.stop.set()
            return {'id': run_id, 'status': 'stop_requested'}

    def _observe_stage(self, run_id, event):
        stage = event_stage(event)
        if stage is not None:
            with self.lock:
                task = self.tasks[run_id]
                if task['stage'] != stage:
                    self._save_task(run_id, change_task(task, stage=stage))

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
        with self.lock:
            return self._run_index()

    def _run_index(self):
        recorded = self.recorded_runs()
        rows = []
        for key, path in sorted(self.paths().items(), reverse=True):
            manifest = json.loads((path/'manifest.json').read_text(encoding='utf8'))
            result = json.loads((path/'result.json').read_text(encoding='utf8')) if (path/'result.json').exists() else {}
            rows.append({'id': key, 'question': manifest['question'], 'model': manifest.get('model', {}).get('name'),
                         'task': self._task_view(key, path, result),
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
        with self.lock:
            return self._read_run(run_id)

    def _read_run(self, run_id):
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
                'task': self._task_view(run_id, path, result),
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

    def list_conversations(self):
        store = self._workspace()
        with self.lock:
            active = self.active
        return {'conversations': [_public_conversation(item) for item in store.list_conversations()],
                'active_run_id': active}

    def create_conversation(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Expected a JSON object')
        datasource_id = payload.get('datasource_id')
        if not isinstance(datasource_id, str) or datasource_id not in self.databases:
            raise ValueError('Data source is not registered')
        store = self._workspace()
        source = store.get_data_source(datasource_id)
        title = payload.get('title', source.name)
        if not isinstance(title, str):
            raise ValueError('title must be a string')
        title = title.strip()
        now = datetime.now(timezone.utc).isoformat()
        from sql_agent_lab.workspace.database import LOCAL_USER_ID, LOCAL_WORKSPACE_ID
        from sql_agent_lab.workspace.models import Conversation
        conversation = Conversation(
            str(uuid4()), LOCAL_WORKSPACE_ID, LOCAL_USER_ID, datasource_id, title, now, now)
        store.add_conversation(conversation)
        return _public_conversation(conversation)

    def conversation_detail(self, conversation_id):
        store = self._workspace()
        conversation = store.get_conversation(str(conversation_id))
        messages = [_public_message(item) for item in store.list_messages(conversation.id)]
        with self.lock:
            runs = [self._run_summary(item) for item in store.list_agent_runs(conversation.id)]
        return {'conversation': _public_conversation(conversation), 'messages': messages, 'runs': runs}

    def post_conversation_message(self, conversation_id, payload):
        if not isinstance(payload, dict):
            raise ValueError('Expected a JSON object')
        store = self._workspace()
        conversation = store.get_conversation(str(conversation_id))
        if payload.get('datasource_id', conversation.datasource_id) != conversation.datasource_id:
            raise ValueError('A conversation is bound to one data source')
        if payload.get('db_id', conversation.datasource_id) != conversation.datasource_id:
            raise ValueError('A conversation is bound to one data source')
        run_id = self.start({
            'conversation_id': conversation.id,
            'question': payload.get('question', ''),
            **({'use_grounding': payload['use_grounding']} if 'use_grounding' in payload else {}),
        })
        task = self.tasks.get(run_id, {})
        if task.get('persistence_error'):
            raise RuntimeError(task['persistence_error'])
        run = store.get_agent_run(run_id)
        if run.conversation_id != conversation.id:
            raise RuntimeError('Conversation was not saved')
        return {'id': run_id, 'conversation_id': conversation.id, 'message_id': run.user_message_id}

    def refresh_profile(self, source_id):
        source = self._source_record(source_id)
        from sql_agent_lab.workspace.database import LOCAL_WORKSPACE_ID
        from sql_agent_lab.workspace.profile import extract_profile, public_profile
        extracted = extract_profile(self.databases[source.id])
        extracted['extracted_at'] = datetime.now(timezone.utc).isoformat()
        stored = {key: extracted[key] for key in (
            'kind', 'tables', 'relationships', 'fingerprint', 'coverage')}
        self._workspace().save_profile(
            source.id, LOCAL_WORKSPACE_ID, extracted['fingerprint'], extracted['extracted_at'],
            json.dumps(stored, ensure_ascii=False))
        return public_profile(source, extracted)

    def list_source_knowledge(self, source_id):
        self._source_record(source_id)
        from sql_agent_lab.workspace.knowledge import list_knowledge
        return {'knowledge': list_knowledge(self._workspace(), self.workspace_db, source_id)}

    def get_source_knowledge(self, source_id, knowledge_id):
        self._source_record(source_id)
        from sql_agent_lab.workspace.knowledge import read_knowledge
        return read_knowledge(self._workspace(), self.workspace_db, source_id, knowledge_id)

    def add_source_knowledge(self, source_id, payload):
        self._source_record(source_id)
        _reject_client_locations(payload)
        from sql_agent_lab.workspace.knowledge import create_knowledge
        return create_knowledge(
            self._workspace(), self.workspace_db, source_id,
            payload.get('title'), payload.get('format'), payload.get('body'))

    def update_source_knowledge(self, source_id, knowledge_id, payload):
        self._source_record(source_id)
        _reject_client_locations(payload)
        from sql_agent_lab.workspace.knowledge import update_knowledge
        return update_knowledge(
            self._workspace(), self.workspace_db, source_id, knowledge_id,
            payload.get('title'), payload.get('format'), payload.get('body'))

    def delete_source_knowledge(self, source_id, knowledge_id):
        self._source_record(source_id)
        from sql_agent_lab.workspace.knowledge import delete_knowledge
        return delete_knowledge(self._workspace(), self.workspace_db, source_id, knowledge_id)

    def _prepare_grounding(self, question, payload):
        """Retrieve only when the request explicitly asks. Default is off."""
        enabled = _use_grounding(payload)
        disabled = {'enabled': False, 'proves_sql': False, 'estimated_api_cost': None,
                    'cost_status': 'not_recorded'}
        if not enabled:
            return {'grounding_enabled': False, 'grounding_block': '', 'grounding': disabled}
        if self.workspace_db is None:
            raise ValueError('Database grounding requires --workspace-db')
        self._source_record(question.db_id)
        started = time.perf_counter()
        profile = self.refresh_profile(question.db_id)
        from sql_agent_lab.workspace.grounding_context import render_grounding
        from sql_agent_lab.workspace.knowledge import read_available_knowledge
        from sql_agent_lab.workspace.retrieval import QUESTION_LIMIT, ground
        documents, skipped = read_available_knowledge(
            self._workspace(), self.workspace_db, question.db_id)
        truncated = len(question.question) > QUESTION_LIMIT
        bundle = ground(question.db_id, profile, documents, question.question[:QUESTION_LIMIT])
        block, summary = render_grounding(bundle, skipped, question_truncated=truncated)
        summary['retrieval_latency_ms'] = round((time.perf_counter() - started) * 1000, 1)
        return {'grounding_enabled': True, 'grounding_block': block, 'grounding': summary}

    def ground_source(self, source_id, payload):
        """Refresh the catalog profile and retrieve names. Does not call the Agent."""
        _reject_client_locations(payload)
        question = payload.get('question', '')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
            raise ValueError('question must be 1–1000 characters')
        profile = self.refresh_profile(source_id)
        from sql_agent_lab.workspace.knowledge import list_knowledge
        from sql_agent_lab.workspace.retrieval import ground
        documents = list_knowledge(self._workspace(), self.workspace_db, source_id)
        return ground(source_id, profile, documents, question.strip())

    def source_context(self, source_id):
        """Stored profile plus user knowledge. Does not change the Agent prompt."""
        source = self._source_record(source_id)
        from sql_agent_lab.workspace.context import read_context
        return read_context(self._workspace(), self.workspace_db, source)

    def _source_record(self, source_id):
        if not isinstance(source_id, str) or source_id not in self.databases:
            raise ValueError('Data source is not registered')
        return self._workspace().get_data_source(source_id)

    def _workspace(self):
        if self.workspace_db is None:
            raise ValueError('Workspace database is not enabled')
        from sql_agent_lab.workspace.database import WorkspaceStore
        return WorkspaceStore(self.workspace_db)

    def _run_summary(self, run):
        path = self.output / run.id
        view = None
        status = run.result_status
        score = run.score
        if (path / 'manifest.json').is_file():
            result = None
            result_path = path / 'result.json'
            if result_path.is_file():
                loaded = json.loads(result_path.read_text(encoding='utf8'))
                if isinstance(loaded, dict):
                    result = loaded
                    status = loaded.get('status') or None
                    if 'score' in loaded:
                        score = loaded.get('score')
            view = self._task_view(run.id, path, result)
        if score in (0.0, 1.0) and not isinstance(score, bool):
            score = int(score)
        elif score not in (None, 0, 1):
            score = None
        from sql_agent_lab.workspace.result_view import public_notice, public_result
        summary = {
            'id': run.id,
            'user_message_id': run.user_message_id,
            'datasource_id': run.datasource_id,
            'state': view['state'] if view else run.state,
            'stage': (view.get('stage') if view else run.stage) or None,
            'started_at': view.get('started_at') if view else run.started_at,
            'updated_at': view.get('updated_at') if view else run.updated_at,
            'ended_at': view.get('ended_at') if view else run.ended_at,
            'stop_requested_at': view.get('stop_requested_at') if view else run.stop_requested_at,
            'error': (view.get('error') if view else run.error) or None,
            'result_status': status,
            'score': score,
            'semantic_review': run.semantic_review,
            'result': public_result(
                path if (path / 'manifest.json').is_file() else None,
                state=view['state'] if view else run.state,
                stage=(view.get('stage') if view else run.stage) or None,
                result=result if (path / 'manifest.json').is_file() else None,
                task_error=(view.get('error') if view else run.error) or None),
        }
        summary['error'] = public_notice(summary['error'])
        if view and view.get('persistence_error'):
            notice = public_notice(view['persistence_error'])
            if notice:
                summary['persistence_error'] = notice
        return summary

    def start(self, payload):
        conversation_id = payload.get('conversation_id')
        if conversation_id is not None:
            if self.workspace_db is None:
                raise ValueError('Workspace database is not enabled')
            if payload.get('question_id') is not None:
                raise ValueError('A conversation message cannot select a benchmark question')
            from sql_agent_lab.workspace.database import WorkspaceStore
            conversation = WorkspaceStore(self.workspace_db).get_conversation(str(conversation_id))
            supplied = payload.get('db_id', payload.get('datasource_id', conversation.datasource_id))
            if 'datasource_id' in payload and payload.get('datasource_id') != conversation.datasource_id:
                raise ValueError('A conversation is bound to one data source')
            if payload.get('db_id') not in (None, conversation.datasource_id):
                raise ValueError('A conversation is bound to one data source')
            if supplied != conversation.datasource_id or conversation.datasource_id not in self.databases:
                raise ValueError('Conversation data source is not registered')
            text = payload.get('question', '')
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
                raise ValueError('Choose a database and enter a question (maximum 4000 characters)')
            question, reference = Question('custom', conversation.datasource_id, text.strip()), None
        elif payload.get('question_id') is not None:
            if self.benchmark is None:
                raise ValueError('BIRD benchmark is not configured')
            question, reference = self.benchmark.question(payload['question_id'])
            if payload.get('db_id') is not None and payload['db_id'] != question.db_id:
                raise ValueError('Benchmark question does not belong to the selected database')
        else:
            db, text = payload.get('db_id'), payload.get('question', '')
            if not isinstance(db, str) or db not in self.databases or not isinstance(text, str) or not 1 <= len(text.strip()) <= 4000:
                raise ValueError('Choose a database and enter a question (maximum 4000 characters)')
            question, reference = Question('custom', db, text.strip()), None
        prepared = self._prepare_grounding(question, payload)
        with self.lock:
            if self.active:
                raise ValueError('A run is already active')
            run_id = datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid4().hex[:6]
            path = self.output / run_id
            path.mkdir()
            if conversation_id is not None:
                self._run_conversation[run_id] = str(conversation_id)
            manifest = {'question': asdict(question), 'model': {'name': 'qwen3:4b-instruct'},
                        'mode': 'local_inference', 'created_at': datetime.now(timezone.utc).isoformat(),
                        'benchmark': reference is not None, 'schema_retrieval': bool(payload.get('retrieval', True)),
                        'semantic_review': bool(payload.get('semantic_review', False)),
                        'semantic_review_contract': CONTRACT if payload.get('semantic_review') else 'disabled',
                        'workflow_mode': 'optional_semantic_review' if payload.get('semantic_review') else 'execution_repair_only',
                        'max_repairs': 1,
                        'semantic_output_tokens': 1600,
                        'cloud_api_calls': 0,
                        'grounding_enabled': prepared['grounding_enabled'],
                        'grounding': prepared['grounding']}
            if prepared['grounding_block']:
                manifest['grounding_block'] = prepared['grounding_block']
            (path/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
            try:
                self._save_task(run_id, new_task())
                self.active = run_id
                self.stop.clear()
                threading.Thread(target=self.work, args=(run_id, question, reference, manifest), daemon=True).start()
            except Exception as error:
                try:
                    self._save_task(run_id, change_task(self.tasks[run_id], state='failed', error=str(error)))
                finally:
                    self.active = None
                raise
        return run_id

    def work(self, run_id, question, reference, manifest):
        """Own the application lifecycle around the unchanged Agent execution."""
        result = {'status': 'failed', 'error': 'Task ended without a final result'}
        try:
            result = self._work(run_id, question, reference, manifest)
        except Exception as error:
            # Includes trace/result persistence errors, not only model errors.
            result = {'status': 'failed', 'error': str(error)}
        finally:
            with self.lock:
                try:
                    self._save_task(run_id, change_task(self.tasks[run_id],
                        state=result_task_state(result), error=result.get('error')))
                except OSError:
                    # _save_task retains the failure in memory for the API.
                    # A restart derives interrupted state from unfinished disk data.
                    pass
                finally:
                    self.active = None

    def _work(self, run_id, question, reference, manifest):
        path = self.output / run_id
        trace = JsonlTrace(path/'trace.jsonl')
        started = time.monotonic()
        writer = TraceEmitter(trace, run_id, question.question_id, started=started)
        def emit(event):
            if event['event'] == 'call_started' and self.stop.is_set():
                raise Stopped('Stopped before the next model call')
            writer(event)
            self._observe_stage(run_id, event)
        result = {'status': 'failed', 'score': None, 'cloud_api_calls': 0}
        agent_latency_ms = None
        try:
            if self.stop.is_set():
                raise Stopped('Stopped before the next tool call')
            emit({'event': 'run_preparing'})
            if manifest.get('grounding_enabled'):
                emit({'event': 'grounding_attached', 'grounding_enabled': True,
                      **{key: manifest['grounding'][key] for key in manifest.get('grounding', {})
                         if key != 'proves_sql'},
                      'model_call': False})
            else:
                emit({'event': 'grounding_skipped', 'grounding_enabled': False, 'model_call': False})
            db = self.databases[question.db_id]
            before = sha256(db)
            # Preserve pinning for BIRD sources, including custom questions on them.
            if self.benchmark and question.db_id in self.benchmark.expected_hashes:
                self.benchmark.verify_database(question.db_id, before)
            raw = SQLiteTools({question.db_id: db}, ToolLimits(timeout_seconds=30), metadata=self.metadata)
            observed = ObservedTools(raw, emit, self.stop)
            with httpx.Client(base_url='http://127.0.0.1:11434', trust_env=False,
                              follow_redirects=False, timeout=600) as client:
                model = LocalOllamaModel(client, 'qwen3:4b-instruct', 16384, num_threads=8, cpu_only=True)
                emit({'event': 'model_ready', 'model': model.metadata})
                config = WorkflowConfig(observed, model, context=ContextPolicy(input_budget=14000,
                    context_window=16384), trace=emit, schema_retrieval=manifest['schema_retrieval'],
                    value_lookup=manifest['schema_retrieval'], semantic_review=manifest.get('semantic_review', False))
                agent = ReferenceMacSQLAgent({role: 'qwen3:4b-instruct' for role in ROLES}, config=config)
                agent.grounding_block = manifest.get('grounding_block') or ''
                agent_started = time.perf_counter()
                sql = agent.run(question)
                agent_latency_ms = round((time.perf_counter() - agent_started) * 1000, 1)
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
            records = ([json.loads(line) for line in (path/'trace.jsonl').read_text(encoding='utf8').splitlines()]
                       if (path/'trace.jsonl').exists() else [])
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
            grounding = dict(manifest.get('grounding') or {})
            grounding['enabled'] = bool(manifest.get('grounding_enabled'))
            grounding['proves_sql'] = False
            grounding['estimated_api_cost'] = None
            if grounding['enabled']:
                grounding['agent_latency_ms'] = agent_latency_ms
            result['grounding'] = grounding
            emit({'event': 'run_finished', **result})
            write_json_atomic(path/'result.json', result)
        return result


def _public_conversation(conversation):
    return {'id': conversation.id, 'title': conversation.title,
            'datasource_id': conversation.datasource_id,
            'created_at': conversation.created_at, 'updated_at': conversation.updated_at}


def _public_message(message):
    return {'id': message.id, 'conversation_id': message.conversation_id, 'seq': message.seq,
            'role': message.role, 'body': message.body, 'created_at': message.created_at}


_STUDIO_ROOT = Path(__file__).resolve().parents[3] / 'prototypes' / 'workspace-ui'
_STUDIO_FILES = {
    '/workspace': 'index.html',
    '/workspace/': 'index.html',
    '/workspace/index.html': 'index.html',
    '/workspace/app.js': 'app.js',
    '/workspace/styles.css': 'styles.css',
}
_STUDIO_TYPES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css'}


def _studio_file(path):
    name = _STUDIO_FILES.get(path)
    if name is None:
        return None
    root = _STUDIO_ROOT.resolve()
    file = (root / name).resolve()
    if file.parent != root or not file.is_file():
        return None
    return file.read_bytes(), _STUDIO_TYPES[file.suffix]


def _match_conversation(path):
    if path == '/api/conversations':
        return 'collection', None
    prefix = '/api/conversations/'
    if not path.startswith(prefix):
        return None, None
    rest = path[len(prefix):]
    if rest.endswith('/messages'):
        conversation_id = rest[:-len('/messages')]
        if conversation_id and '/' not in conversation_id:
            return 'messages', conversation_id
    elif rest and '/' not in rest:
        return 'item', rest
    return None, None


_CLIENT_LOCATIONS = frozenset({
    'path', 'paths', 'filename', 'file', 'directory', 'storage_key', 'uri', 'url',
})


def _use_grounding(payload):
    if not isinstance(payload, dict) or 'use_grounding' not in payload:
        return False
    value = payload['use_grounding']
    if not isinstance(value, bool):
        raise ValueError('use_grounding must be true or false')
    return value


def _reject_client_locations(payload):
    if not isinstance(payload, dict):
        raise ValueError('Expected a JSON object')
    for key in payload:
        if str(key).lower() in _CLIENT_LOCATIONS:
            raise ValueError('A server path cannot be supplied')


def _match_source(path):
    prefix = '/api/sources/'
    if not path.startswith(prefix):
        return None
    parts = path[len(prefix):].split('/')
    if len(parts) == 2 and parts[0] and parts[1] in ('profile', 'knowledge', 'context', 'grounding'):
        return parts[0], parts[1], None
    if len(parts) == 3 and parts[1] == 'knowledge' and parts[0] and parts[2]:
        return parts[0], 'knowledge_item', parts[2]
    return None


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
                    return self.send(Path(__file__).with_name('user_ui.html').read_bytes(), content_type='text/html')
                if url.path == '/observatory':
                    return self.send(Path(__file__).with_suffix('.html').read_bytes(), content_type='text/html')
                if url.path == '/analysis':
                    return self.send(Path(__file__).with_name('analysis.html').read_bytes(), content_type='text/html')
                studio = _studio_file(url.path)
                if studio is not None:
                    body, mime = studio
                    return self.send(body, content_type=mime)
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
                    return self.send(app.catalog())
                if url.path == '/api/runs':
                    return self.send(app.run_index())
                if url.path == '/api/run':
                    return self.send(app.read_run(parse_qs(url.query).get('id', [''])[0]))
                kind, conversation_id = _match_conversation(url.path)
                if kind in ('collection', 'item'):
                    if app.workspace_db is None:
                        return self.send({'error': 'Not found'}, 404)
                    if kind == 'collection':
                        return self.send(app.list_conversations())
                    return self.send(app.conversation_detail(conversation_id))
                source = _match_source(url.path)
                if source is not None:
                    if app.workspace_db is None:
                        return self.send({'error': 'Not found'}, 404)
                    source_id, source_kind, knowledge_id = source
                    if source_kind == 'profile':
                        return self.send(app.refresh_profile(source_id))
                    if source_kind == 'knowledge':
                        return self.send(app.list_source_knowledge(source_id))
                    if source_kind == 'knowledge_item':
                        return self.send(app.get_source_knowledge(source_id, knowledge_id))
                    return self.send(app.source_context(source_id))
                self.send({'error': 'Not found'}, 404)
            except (ValueError, OSError) as error:
                self.send({'error': str(error)}, 400)

        def _json_body(self, limit):
            host = self.headers.get('Host', '')
            expected = f'127.0.0.1:{self.server.server_port}'
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > limit:
                raise ValueError('Request too large')
            body = self.rfile.read(size)
            if host != expected or self.headers.get('Origin', 'http://' + expected) != 'http://' + expected:
                self.send({'error': 'Local same-origin requests only'}, 403)
                return None
            payload = json.loads(body or b'{}')
            if not isinstance(payload, dict):
                raise ValueError('Expected a JSON object')
            return payload

        def do_POST(self):
            # Reject cross-origin browser requests and DNS rebinding hostnames.
            try:
                path = urlparse(self.path).path
                source = _match_source(path)
                limit = 48000 if source and source[1] == 'knowledge' else 20000
                payload = self._json_body(limit)
                if payload is None:
                    return None
                if source is not None and source[1] == 'knowledge':
                    if app.workspace_db is None:
                        return self.send({'error': 'Not found'}, 404)
                    return self.send(app.add_source_knowledge(source[0], payload))
                if source is not None and source[1] == 'grounding':
                    if app.workspace_db is None:
                        return self.send({'error': 'Not found'}, 404)
                    return self.send(app.ground_source(source[0], payload))
                kind, conversation_id = _match_conversation(path)
                if kind in ('collection', 'messages'):
                    if app.workspace_db is None:
                        return self.send({'error': 'Not found'}, 404)
                    if kind == 'collection':
                        return self.send(app.create_conversation(payload))
                    return self.send(app.post_conversation_message(conversation_id, payload))
                if self.path == '/api/start':
                    return self.send({'id': app.start(payload)})
                if self.path == '/api/stop':
                    return self.send(app.request_stop(payload.get('id')))
                self.send({'error': 'Not found'}, 404)
            except (ValueError, TypeError) as error:
                self.send({'error': str(error)}, 400)
            except (OSError, RuntimeError) as error:
                self.send({'error': str(error)}, 500)

        def do_PUT(self):
            try:
                source = _match_source(urlparse(self.path).path)
                payload = self._json_body(48000)
                if payload is None:
                    return None
                if source is None or source[1] != 'knowledge_item':
                    return self.send({'error': 'Not found'}, 404)
                if app.workspace_db is None:
                    return self.send({'error': 'Not found'}, 404)
                self.send(app.update_source_knowledge(source[0], source[2], payload))
            except (ValueError, TypeError) as error:
                self.send({'error': str(error)}, 400)
            except (OSError, RuntimeError) as error:
                self.send({'error': str(error)}, 500)

        def do_DELETE(self):
            try:
                host = self.headers.get('Host', '')
                expected = f'127.0.0.1:{self.server.server_port}'
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 20000:
                    raise ValueError('Request too large')
                if size:
                    self.rfile.read(size)
                if host != expected or self.headers.get('Origin', 'http://' + expected) != 'http://' + expected:
                    return self.send({'error': 'Local same-origin requests only'}, 403)
                source = _match_source(urlparse(self.path).path)
                if source is None or source[1] != 'knowledge_item':
                    return self.send({'error': 'Not found'}, 404)
                if app.workspace_db is None:
                    return self.send({'error': 'Not found'}, 404)
                self.send(app.delete_source_knowledge(source[0], source[2]))
            except (ValueError, TypeError) as error:
                self.send({'error': str(error)}, 400)
            except (OSError, RuntimeError) as error:
                self.send({'error': str(error)}, 500)
    return Handler


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, help='Optional pinned BIRD benchmark directory')
    parser.add_argument('--sources', type=Path, help='Local SQLite source registry JSON')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--history', type=Path)
    parser.add_argument('--analysis-dir', type=Path, help='Read-only local historical analysis exports')
    parser.add_argument('--recorded-batch', type=Path, action='append', default=[],
                        help='Read an existing API batch without invoking models or evaluation')
    parser.add_argument('--workspace-db', type=Path, default=None,
                        help='Optional workspace metadata SQLite file. Omit to leave startup unchanged.')
    parser.add_argument('--port', type=int, default=8765)
    return parser


def main():
    args = argument_parser().parse_args()
    app = Observatory(args.data_dir, args.output_dir, args.metadata, args.history, args.analysis_dir,
                      args.recorded_batch, sources=args.sources, workspace_db=args.workspace_db)
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
