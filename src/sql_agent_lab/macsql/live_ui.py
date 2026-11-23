"""Local SQLite observatory with optional BIRD evaluation; startup invokes no models."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import inspect
import json
from pathlib import Path
import sqlite3
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
from .trace_support import TraceEmitter, review_timing, token_totals
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
                if self.stop.is_set():
                    raise Stopped('Stopped during the tool call')
            except Exception as error:
                self.emit({'event': 'tool_failed', 'tool': name, 'tool_call_id': call_id,
                           'error': str(error), 'elapsed_seconds': time.monotonic() - started})
                if self.stop.is_set() and not isinstance(error, Stopped):
                    raise Stopped('Stopped during the tool call') from error
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
        self.upload_registry = self.output / 'uploaded-sources.json'
        if self.upload_registry.exists():
            self.sources.load(self.upload_registry)
        self.benchmark = BirdBenchmark(self.data, self.sources) if self.data else None
        self.databases = self.sources.databases
        if not self.databases and workspace_db is None:
            raise ValueError('Configure at least one SQLite source (--sources) or BIRD dataset (--data-dir)')
        # SourceRegistry remains the authority for SQLite paths. The optional
        # metadata file receives only the public catalog.
        self.workspace_db = None
        self.workspace_store = None
        if workspace_db is not None:
            from sql_agent_lab.workspace.database import sync_registered_sources
            self.workspace_store = sync_registered_sources(workspace_db, self.sources.catalog())
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
        self._profiles = {}
        self._profile_lock = threading.Lock()

    def upload_database(self, payload):
        import base64
        import binascii
        from .data_sources import SQLiteSource
        from sql_agent_lab.workspace.profile import extract_profile
        from sql_agent_lab.workspace.database import LOCAL_WORKSPACE_ID
        from sql_agent_lab.workspace.knowledge import create_knowledge, delete_knowledge
        from sql_agent_lab.workspace.models import data_source_from_catalog
        from .task_lifecycle import utc_now
        store = self._workspace()
        name = payload.get('name', '')
        description = payload.get('description', '')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('请输入数据库名称（最多100字）')
        if not isinstance(description, str) or len(description) > 10000:
            raise ValueError('业务说明最多10000字')
        try:
            data = base64.b64decode(payload.get('data', ''), validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise ValueError('数据库文件编码无效')
        if not data.startswith(b'SQLite format 3\x00') or len(data) > 20 * 1024 * 1024:
            raise ValueError('请上传不超过20 MB的有效 SQLite 文件')
        with self.lock, ExitStack() as rollback:
            if self.active:
                raise ValueError('请等待当前查询结束后添加数据库')
            source_id = 'upload_' + uuid4().hex
            directory = self.output / 'databases'
            directory.mkdir(exist_ok=True)
            target = directory / (source_id + '.sqlite')
            rollback.callback(target.unlink, missing_ok=True)
            target.write_bytes(data)
            try:
                profile = extract_profile(target)
            except ValueError as error:
                raise ValueError('数据库无法读取，请上传完整、未加密的 SQLite 文件') from error
            source = SQLiteSource(source_id, name.strip(), target)
            updated = self.sources.with_source(source)
            bindings = updated.databases
            document = json.loads(self.upload_registry.read_text(encoding='utf8')) if self.upload_registry.exists() else {'sources': []}
            document['sources'].append({'id': source_id, 'name': name.strip(), 'path': str(target), 'kind': 'sqlite'})
            store.add_data_source(data_source_from_catalog(
                LOCAL_WORKSPACE_ID, source.public(), secret_ref='sources:' + source_id, created_at=utc_now()))
            rollback.callback(store.discard_unpublished_source, source_id)
            if description.strip():
                note = create_knowledge(store, self.workspace_db, source_id, '业务说明', 'text', description.strip())
                rollback.callback(delete_knowledge, store, self.workspace_db, source_id, note['id'])
            write_json_atomic(self.upload_registry, document)
            self.sources, self.databases = updated, bindings
            rollback.pop_all()
            return {'source': source.public(), 'profile': profile}

    def catalog(self):
        catalog = self.sources.catalog()
        return {'databases': [s['id'] for s in catalog], 'sources': catalog,
                'benchmark_available': self.benchmark is not None,
                'questions': [asdict(q) for q, _ in self.dataset],
                'semantic_review_contract': CONTRACT, 'semantic_output_tokens': 1600}

    def _save_task(self, run_id, task, *, project=True):
        """Called under self.lock so stop requests and worker updates cannot race."""
        task = {key: value for key, value in task.items() if key != 'persistence_error'}
        self.tasks[run_id] = task
        try:
            write_json_atomic(self.output / run_id / 'task.json', task)
        except OSError as error:
            task['persistence_error'] = str(error)
            raise
        else:
            if project:
                self._project_workspace(run_id)

    def _project_workspace(self, run_id):
        """Copy a saved run into the optional workspace database.

        The run directory is already the source of truth. A database error is
        reported on the in-memory task and does not rewrite that directory.
        """
        if self.workspace_db is None:
            return
        try:
            from sql_agent_lab.workspace.database import record_saved_run
            path = self.output / run_id
            manifest = json.loads((path / 'manifest.json').read_text(encoding='utf8'))
            task = json.loads((path / 'task.json').read_text(encoding='utf8'))
            result_path = path / 'result.json'
            result = json.loads(result_path.read_text(encoding='utf8')) if result_path.is_file() else None
            record_saved_run(
                self._workspace(), run_id=run_id, manifest=manifest, task=task,
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

    def paths(self, recorded=None):
        paths = {p.name: p for p in self.output.iterdir() if p.is_dir() and (p/'manifest.json').exists()}
        if self.history:
            for p in self.history.glob('local-pilot-*'):
                if (p/'manifest.json').exists():
                    paths['history-' + p.name] = p
        if recorded is None:
            recorded = self.recorded_runs()
        paths.update({key: value['path'] for key, value in recorded.items()})
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
        for key, path in sorted(self.paths(recorded).items(), reverse=True):
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

    def user_runs(self):
        from .user_view import public_question, user_task
        rows = []
        with self.lock:
            active = self.active
            for key, path in sorted(self.paths().items(), reverse=True):
                manifest = json.loads((path / 'manifest.json').read_text(encoding='utf8'))
                question = public_question(manifest)
                if question['question_id'] != 'custom':
                    continue
                result_path = path / 'result.json'
                result = json.loads(result_path.read_text(encoding='utf8')) if result_path.is_file() else {}
                rows.append({'id': key, 'question': question, 'status': result.get('status'),
                             'task': user_task(self._task_view(key, path, result))})
                if len(rows) == 20:
                    break
        return {'active': active, 'runs': rows}

    def read_run(self, run_id, *, user=False):
        with self.lock:
            return self._read_run(run_id, user=user)

    def _read_run(self, run_id, *, user=False):
        records = self.recorded_runs()
        path = self.paths(records).get(run_id)
        if path is None:
            raise ValueError('Unknown run')
        result = json.loads((path/'result.json').read_text(encoding='utf8')) if (path/'result.json').exists() else None
        manifest = json.loads((path/'manifest.json').read_text(encoding='utf8'))
        task = self._task_view(run_id, path, result)
        if user:
            from .user_view import user_run
            return user_run(path, run_id, manifest, result, task, active=self.active == run_id,
                            stop_requested=self.active == run_id and self.stop.is_set())
        events = []
        if (path/'trace.jsonl').exists():
            for line in (path/'trace.jsonl').read_text(encoding='utf8').splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # A concurrent append may not have written its final newline yet.
        evaluation = (json.loads((path/'evaluation-evidence.json').read_text(encoding='utf8'))
                      if result is not None and (path/'evaluation-evidence.json').exists() else None)
        recorded = records.get(run_id)
        # Normalize the display timestamp without changing the saved trace.
        events = [{**e, 'elapsed': e['elapsed_seconds_since_start']}
                  if 'elapsed' not in e and 'elapsed_seconds_since_start' in e else e for e in events]
        return {'id': run_id, 'historical': run_id.startswith('history-') or recorded is not None,
                'task': task,
                'recorded': recorded is not None,
                'recorded_model_version': recorded['protocol'].get('documented_version') if recorded else None,
                'manifest': manifest,
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
        with self.lock:
            messages = [_public_message(item) for item in store.list_messages(conversation.id)]
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
            'use_grounding': True,
            'use_conversation_context': True,
            **{key: payload[key] for key in ('use_grounding', 'retrieval_strategy', 'use_conversation_context') if key in payload},
        })
        run = store.get_agent_run(run_id)
        if run.conversation_id != conversation.id:
            raise RuntimeError('Conversation was not saved')
        return {'id': run_id, 'conversation_id': conversation.id, 'message_id': run.user_message_id}

    def refresh_profile(self, source_id):
        source = self._source_record(source_id)
        from sql_agent_lab.workspace.database import LOCAL_WORKSPACE_ID
        from sql_agent_lab.workspace.profile import read_profile_if_changed, public_profile
        with self._profile_lock:
            previous, cached = self._profiles.get(source.id, (None, None))
            stamp, extracted = read_profile_if_changed(self.databases[source.id], previous)
            if extracted is None:
                return deepcopy(cached)
            serialized = json.dumps(extracted, ensure_ascii=False)
            extracted['extracted_at'] = datetime.now(timezone.utc).isoformat()
            self._workspace().save_profile(
                source.id, LOCAL_WORKSPACE_ID, extracted['fingerprint'], extracted['extracted_at'], serialized)
            profile = public_profile(source, extracted)
            self._profiles[source.id] = (stamp, profile)
            return deepcopy(profile)

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
            payload.get('title'), payload.get('format'), payload.get('body'),
            expected_version=payload.get('expected_version'))

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
        from sql_agent_lab.workspace.knowledge_index import WorkspaceKnowledgeIndex
        from sql_agent_lab.workspace.retrieval import QUESTION_LIMIT, ground
        documents, skipped = read_available_knowledge(
            self._workspace(), self.workspace_db, question.db_id)
        truncated = len(question.question) > QUESTION_LIMIT
        strategy = _retrieval_strategy(payload)
        index = None
        if strategy != 'keyword':
            index = WorkspaceKnowledgeIndex(self._workspace(), self.workspace_db)
        bundle = ground(
            question.db_id, profile, documents, question.question[:QUESTION_LIMIT],
            strategy=strategy, knowledge_index=index)
        block, summary = render_grounding(bundle, skipped, question_truncated=truncated)
        summary['retrieval_latency_ms'] = round((time.perf_counter() - started) * 1000, 1)
        return {'grounding_enabled': True, 'grounding_block': block, 'grounding': summary}

    def ground_source(self, source_id, payload):
        """Refresh the catalog profile and retrieve names. Does not call the Agent."""
        _reject_client_locations(payload)
        question = payload.get('question', '')
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1000:
            raise ValueError('question must be 1–1000 characters')
        strategy = _retrieval_strategy(payload)
        profile = self.refresh_profile(source_id)
        from sql_agent_lab.workspace.knowledge import list_knowledge
        from sql_agent_lab.workspace.knowledge_index import WorkspaceKnowledgeIndex
        from sql_agent_lab.workspace.retrieval import ground
        documents = list_knowledge(self._workspace(), self.workspace_db, source_id)
        index = None
        if strategy != 'keyword':
            index = WorkspaceKnowledgeIndex(self._workspace(), self.workspace_db)
        started = time.perf_counter()
        bundle = ground(
            source_id, profile, documents, question.strip(),
            strategy=strategy, knowledge_index=index)
        bundle = dict(bundle)
        bundle['retrieval_latency_ms'] = round((time.perf_counter() - started) * 1000, 1)
        return bundle

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
        return self.workspace_store

    def _run_summary(self, run):
        path = self.output / run.id
        view = None
        result = None
        status = run.result_status
        score = run.score
        managed = (path / 'manifest.json').is_file()
        if managed:
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
                path if managed else None,
                state=view['state'] if view else run.state,
                stage=(view.get('stage') if view else run.stage) or None,
                result=result,
                task_error=(view.get('error') if view else run.error) or None),
        }
        summary['error'] = public_notice(summary['error'])
        if view and view.get('persistence_error'):
            notice = public_notice(view['persistence_error'])
            if notice:
                summary['persistence_error'] = notice
        return summary

    def start(self, payload):
        for option in ('retrieval', 'semantic_review'):
            if option in payload and not isinstance(payload[option], bool):
                raise ValueError(f'{option} must be a boolean')
        conversation_id = payload.get('conversation_id')
        if conversation_id is not None:
            if self.workspace_db is None:
                raise ValueError('Workspace database is not enabled')
            if payload.get('question_id') is not None:
                raise ValueError('A conversation message cannot select a benchmark question')
            conversation = self._workspace().get_conversation(str(conversation_id))
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
        use_history = payload.get('use_conversation_context', False)
        if not isinstance(use_history, bool):
            raise ValueError('use_conversation_context must be a boolean')
        if use_history and conversation_id is None:
            raise ValueError('Conversation context requires a conversation')
        with self.lock:
            if self.active:
                raise ValueError('A run is already active')
        prepared = self._prepare_grounding(question, payload)
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
                        'cloud_api_calls': 0,
                        'grounding_enabled': prepared['grounding_enabled'],
                        'grounding': prepared['grounding']}
            if use_history:
                manifest['conversation_context_enabled'] = True
                manifest['conversation_id'] = str(conversation_id)
            if prepared['grounding_block']:
                manifest['grounding_block'] = prepared['grounding_block']
            write_json_atomic(path/'manifest.json', manifest)
            if conversation_id is not None:
                self._run_conversation[run_id] = str(conversation_id)
            attached = False
            try:
                self._save_task(run_id, new_task())
                failure = self.tasks[run_id].get('persistence_error')
                if conversation_id is not None and failure:
                    raise RuntimeError(failure)
                attached = True
                self.active = run_id
                self.stop.clear()
                threading.Thread(target=self.work, args=(run_id, question, reference, manifest), daemon=True).start()
            except Exception as error:
                try:
                    self._save_task(run_id, change_task(self.tasks[run_id], state='failed', error=str(error)),
                                    project=attached or conversation_id is None)
                finally:
                    self._run_conversation.pop(run_id, None)
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
                    self._run_conversation.pop(run_id, None)
                    self.active = None

    def _prepare_followup(self, conversation_id, run_id, question, emit):
        from sql_agent_lab.workspace.answer_checks import (
            NeedsClarification, clarification)
        from sql_agent_lab.workspace.query_state import resolve_filter_followup
        from sql_agent_lab.workspace.conversation_context import (
            build_conversation_context, explicitly_starts_topic, turns_from_public_records)

        # A rendered turn uses at least 80 bytes. 32 candidates exceed the
        # default 1600-byte prompt budget even before headings or question text.
        pairs = self._workspace().recent_message_runs(conversation_id, exclude_run_id=run_id, limit=32)
        messages = [_public_message(message) for message, _ in pairs]
        runs = [self._run_summary(run) for _, run in pairs]
        history = turns_from_public_records(
            messages, datasource_id=question.db_id, runs=runs)
        context = build_conversation_context(conversation_id=conversation_id,
            datasource_id=question.db_id, question=question.question, history=history)
        prompt = clarification(question.question, context['turns'])
        if prompt:
            raise NeedsClarification(prompt)
        text = question.question
        if messages and not explicitly_starts_topic(text):
            previous = messages[-1]
            if runs[-1]['result']['needs_clarification']:
                text = '原问题：' + previous['body'][:1000] + '\n用户补充：' + text
        text, expected_sql = resolve_filter_followup(text, context['turns'])
        if text != question.question:
            # Derived state and clarification supplements share the history budget.
            if len(text.encode('utf-8')) > context['budget']['limit']:
                raise NeedsClarification('请补充完整条件：上一轮查询条件过长，请重新说明本次指标和筛选条件。')
            context = build_conversation_context(conversation_id=conversation_id,
                datasource_id=question.db_id, question=text, history=history)
        emit({'event': 'conversation_context_attached', 'budget': context['budget']})
        return replace(question, question=text), context['history_block'], expected_sql

    def _work(self, run_id, question, reference, manifest):
        path = self.output / run_id
        trace = JsonlTrace(path/'trace.jsonl')
        started = time.monotonic()
        writer = TraceEmitter(trace, run_id, question.question_id, started=started)
        latest = {}
        calls = []
        def emit(event):
            if event['event'] == 'call_started' and self.stop.is_set():
                raise Stopped('Stopped before the next model call')
            record = writer(event)
            kind = event['event']
            if kind in ('workflow_completed', 'semantic_review_completed', 'semantic_revision_guard'):
                latest[kind] = record
            if kind in ('call_started', 'call_completed', 'call_failed'):
                calls.append({k: event.get(k) for k in ('event', 'usage_complete', 'input_tokens', 'output_tokens')})
            self._observe_stage(run_id, event)
        from sql_agent_lab.workspace.answer_checks import NeedsClarification
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
            history_block = ''
            expected_sql = None
            if manifest.get('conversation_context_enabled'):
                from sql_agent_lab.workspace.chat_intent import direct_answer
                answer = direct_answer(question.question, lambda: self.refresh_profile(question.db_id),
                    lambda: self.list_source_knowledge(question.db_id)['knowledge'])
                if answer:
                    result.update(status='completed', answer=answer)
                    return result
                question, history_block, expected_sql = self._prepare_followup(
                    manifest['conversation_id'], run_id, question, emit)
            if self.stop.is_set():
                raise Stopped('Stopped before preparing the model')
            db = self.databases[question.db_id]
            before = None
            # Preserve pinning for BIRD sources, including custom questions on them.
            if self.benchmark and question.db_id in self.benchmark.expected_hashes:
                before = sha256(db)
                self.benchmark.verify_database(question.db_id, before)
            raw = SQLiteTools({question.db_id: db}, ToolLimits(timeout_seconds=30),
                              metadata=self.metadata, cancelled=self.stop.is_set)
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
                agent.conversation_block = history_block
                agent_started = time.perf_counter()
                if hasattr(agent, 'run_with_feedback'):
                    sql, feedback = agent.run_with_feedback(question)
                else:
                    # Older adapters return only SQL. Apply the same strict prepare check.
                    sql = agent.run(question)
                    if self.stop.is_set():
                        raise Stopped('Stopped after the scheduled operation completed')
                    feedback = None
                if expected_sql is not None:
                    from sql_agent_lab.workspace.query_state import matches_followup_sql
                    matches = matches_followup_sql(sql, expected_sql)
                    emit({'event': 'followup_sql_checked', 'matches': matches})
                    if not matches:
                        sql, feedback = expected_sql, None
                        # Workflow reports describe the discarded candidate.
                        latest.clear()
                if feedback is None:
                    feedback = observed.compile_sql_readonly(question.db_id, sql)
                    if feedback['status'] == 'ok':
                        feedback = observed.execute_sql_readonly(question.db_id, sql)
                    if expected_sql is not None and not matches:
                        emit({'event': 'followup_sql_repaired', 'status': feedback['status']})
                        if feedback['status'] != 'ok':
                            raise NeedsClarification('请补充完整条件：无法按上一轮口径完成这次查询，请重新说明指标和筛选条件。')
                agent_latency_ms = round((time.perf_counter() - agent_started) * 1000, 1)
                if self.stop.is_set():
                    raise Stopped('Stopped after the scheduled operation completed')
                result.update(sql=sql, feedback=feedback, status='completed' if feedback['status']=='ok' else 'failed')
                if reference:
                    emit({'event': 'evaluation_started', 'note': 'Reference SQL is accessible only to the evaluator'})
                    evaluator = EvaluationSession(raw,reference,before,artifact=path/'evaluation-evidence.json',emit=emit)
                    evaluation = evaluator.score(sql,'submitted')
                    result.update(score=evaluation['score'],evaluation_status=evaluation['status'],
                                  evaluation_evidence_file='evaluation-evidence.json')
                    if evaluation['status']=='error': result['status']='evaluation_failed'
                    emit({'event':'evaluation_completed','score':evaluation['score'],'status':evaluation['status']})
                result['database_unchanged'] = sha256(db) == before if before is not None else None
        except NeedsClarification as error:
            result.update(status='completed', clarification=str(error))
        except Stopped as error:
            result.update(status='stopped', error=str(error))
        except Exception as error:
            result.update(status='failed', error_type=type(error).__name__, error=str(error))
        finally:
            completed = latest.get('workflow_completed', {})
            result['tokens'] = token_totals(calls)
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
            result['semantic_report'] = latest.get('semantic_review_completed', {}).get('report')
            result['semantic_guard'] = latest.get('semantic_revision_guard')
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


_STUDIO_ROOT = Path(__file__).resolve().with_name('studio')
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
# A supplementary Unicode character can occupy twelve bytes in escaped JSON.
_KNOWLEDGE_REQUEST_BYTES = 300 * 1024


def _use_grounding(payload):
    if not isinstance(payload, dict) or 'use_grounding' not in payload:
        return False
    value = payload['use_grounding']
    if not isinstance(value, bool):
        raise ValueError('use_grounding must be true or false')
    return value


def _retrieval_strategy(payload):
    """Default keyword. An omitted field does not change the current algorithm."""
    if not isinstance(payload, dict) or 'retrieval_strategy' not in payload:
        return 'keyword'
    value = payload['retrieval_strategy']
    if value == 'embedding':
        raise ValueError('embedding retrieval is not configured')
    if value not in ('keyword', 'bm25', 'hybrid'):
        raise ValueError('retrieval_strategy must be keyword, bm25, or hybrid')
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
            try:
                self.send_response(status)
                self.send_header('Content-Type', content_type + '; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except ConnectionError:
                # Navigation can drop an in-flight polling request.
                self.close_connection = True

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
                        return self.send({'error': 'No analysis directory is configured. Pass --analysis-dir with a local report directory.'}, 404)
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
                if url.path == '/api/user/runs':
                    return self.send(app.user_runs())
                if url.path == '/api/user/run':
                    return self.send(app.read_run(parse_qs(url.query).get('id', [''])[0], user=True))
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
            except sqlite3.Error:
                self.send({'error': '工作区存储暂不可用，请稍后重试。'}, 500)

        def _local_request(self):
            host = self.headers.get('Host', '')
            expected = f'127.0.0.1:{self.server.server_port}'
            if host == expected and self.headers.get('Origin', 'http://' + expected) == 'http://' + expected:
                return True
            self.send({'error': 'Local same-origin requests only'}, 403)
            return False

        def _json_body(self, limit):
            if not self._local_request():
                return None
            size = int(self.headers.get('Content-Length', '0'))
            if size < 0 or size > limit:
                raise ValueError('Request too large')
            body = self.rfile.read(size)
            payload = json.loads(body or b'{}')
            if not isinstance(payload, dict):
                raise ValueError('Expected a JSON object')
            return payload

        def do_POST(self):
            # Reject cross-origin browser requests and DNS rebinding hostnames.
            try:
                path = urlparse(self.path).path
                source = _match_source(path)
                limit = 64 * 1024
                if path == '/api/sources/upload':
                    limit = 29 * 1024 * 1024
                elif source and source[1] == 'knowledge':
                    limit = _KNOWLEDGE_REQUEST_BYTES
                payload = self._json_body(limit)
                if payload is None:
                    return None
                if path == '/api/sources/upload':
                    if app.workspace_db is None:
                        return self.send({'error': 'Not found'}, 404)
                    return self.send(app.upload_database(payload))
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
            except sqlite3.Error:
                self.send({'error': '工作区存储暂不可用，请稍后重试。'}, 500)
            except (OSError, RuntimeError) as error:
                self.send({'error': str(error)}, 500)

        def do_PUT(self):
            try:
                source = _match_source(urlparse(self.path).path)
                payload = self._json_body(_KNOWLEDGE_REQUEST_BYTES)
                if payload is None:
                    return None
                if source is None or source[1] != 'knowledge_item':
                    return self.send({'error': 'Not found'}, 404)
                if app.workspace_db is None:
                    return self.send({'error': 'Not found'}, 404)
                self.send(app.update_source_knowledge(source[0], source[2], payload))
            except (ValueError, TypeError) as error:
                self.send({'error': str(error)}, 400)
            except sqlite3.Error:
                self.send({'error': '工作区存储暂不可用，请稍后重试。'}, 500)
            except (OSError, RuntimeError) as error:
                self.send({'error': str(error)}, 500)

        def do_DELETE(self):
            try:
                if not self._local_request():
                    return
                size = int(self.headers.get('Content-Length', '0'))
                if size < 0 or size > 20000:
                    raise ValueError('Request too large')
                if size:
                    self.rfile.read(size)
                source = _match_source(urlparse(self.path).path)
                if source is None or source[1] != 'knowledge_item':
                    return self.send({'error': 'Not found'}, 404)
                if app.workspace_db is None:
                    return self.send({'error': 'Not found'}, 404)
                self.send(app.delete_source_knowledge(source[0], source[2]))
            except (ValueError, TypeError) as error:
                self.send({'error': str(error)}, 400)
            except sqlite3.Error:
                self.send({'error': '工作区存储暂不可用，请稍后重试。'}, 500)
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
    landing = '/workspace/' if args.workspace_db else '/'
    print(f'SQL Agent Lab http://127.0.0.1:{server.server_port}{landing}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        app.stop.set()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
