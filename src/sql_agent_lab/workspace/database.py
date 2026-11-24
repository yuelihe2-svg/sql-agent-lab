"""SQLite metadata for one local workspace.

User databases are never opened here. Callers pass the application file path.
`live_ui.main` does not call this module.
"""
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
import re
import sqlite3
import uuid

from sql_agent_lab.macsql.task_lifecycle import utc_now

from .knowledge_index import (
    FTS_CREATE_SQL, FTS_TABLES, delete_index, fts5_available, search_index, upsert_index,
)
from .models import (
    AgentRun, Conversation, DataSource, Message, User, Workspace, data_source_from_catalog,
)


SCHEMA_VERSION = 6
# One implicit local workspace. Restarts reuse this id instead of inserting another.
LOCAL_WORKSPACE_ID = '5c6a1e0a-7b34-4d5e-9f10-0a1b2c3d4e5f'
LOCAL_WORKSPACE_NAME = 'Local'
# One implicit local user. This is not an account system.
LOCAL_USER_ID = '6d7b2f1b-8c45-4e6f-a021-1b2c3d4e5f60'
LOCAL_USER_NAME = 'Local'
APP_TABLES = frozenset({
    'schema_migrations', 'workspace', 'app_user', 'membership', 'data_source',
    'data_source_grant', 'conversation', 'message', 'agent_run', 'audit_event',
    'database_profile', 'knowledge_document', 'deleted_conversation', 'organization_identity', *FTS_TABLES,
})
# Columns that would copy a user database, a credential, or gold SQL.
FORBIDDEN_COLUMNS = frozenset({
    'path', 'password', 'passwd', 'secret', 'credential', 'sql', 'gold_sql',
    'reference_sql', 'host', 'port', 'filename', 'dsn',
})

_MIGRATIONS = ((1, """
CREATE TABLE IF NOT EXISTS workspace (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    local_only INTEGER NOT NULL CHECK (local_only = 1)
);
CREATE TABLE IF NOT EXISTS app_user (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS membership (
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    user_id TEXT NOT NULL REFERENCES app_user(id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'analyst', 'developer')),
    PRIMARY KEY (workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS data_source (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('sqlite', 'postgres', 'mysql')),
    read_only INTEGER NOT NULL CHECK (read_only = 1),
    status TEXT NOT NULL CHECK (status IN ('ready', 'unavailable')),
    secret_ref TEXT,
    schema_fingerprint TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_source_grant (
    user_id TEXT NOT NULL REFERENCES app_user(id),
    datasource_id TEXT NOT NULL REFERENCES data_source(id),
    can_query INTEGER NOT NULL CHECK (can_query IN (0, 1)),
    PRIMARY KEY (user_id, datasource_id)
);
CREATE TABLE IF NOT EXISTS conversation (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    user_id TEXT NOT NULL REFERENCES app_user(id),
    datasource_id TEXT NOT NULL REFERENCES data_source(id),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS message (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversation(id),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool_summary')),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS agent_run (
    id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversation(id),
    user_message_id TEXT REFERENCES message(id),
    datasource_id TEXT NOT NULL REFERENCES data_source(id),
    state TEXT NOT NULL CHECK (state IN (
        'running', 'stopping', 'completed', 'failed', 'stopped', 'interrupted', 'unknown')),
    stage TEXT,
    started_at TEXT,
    updated_at TEXT,
    ended_at TEXT,
    stop_requested_at TEXT,
    error TEXT,
    result_status TEXT CHECK (
        result_status IS NULL OR result_status IN (
            'completed', 'needs_review', 'failed', 'evaluation_failed', 'stopped')),
    score INTEGER CHECK (score IS NULL OR score IN (0, 1)),
    semantic_review INTEGER NOT NULL CHECK (semantic_review IN (0, 1)),
    artifact_dir TEXT
);
CREATE TABLE IF NOT EXISTS audit_event (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    actor_user_id TEXT REFERENCES app_user(id),
    workspace_id TEXT REFERENCES workspace(id),
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'denied')),
    detail TEXT NOT NULL
);
"""), (2, """
CREATE TABLE IF NOT EXISTS database_profile (
    datasource_id TEXT PRIMARY KEY REFERENCES data_source(id),
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    fingerprint TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    profile_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_document (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspace(id),
    datasource_id TEXT NOT NULL REFERENCES data_source(id),
    title TEXT NOT NULL,
    format TEXT NOT NULL CHECK (format IN ('markdown', 'text', 'json')),
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    provenance TEXT NOT NULL CHECK (provenance = 'user'),
    storage_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""), (3, FTS_CREATE_SQL + ';'), (4, """
CREATE INDEX IF NOT EXISTS agent_run_conversation_start ON agent_run(conversation_id, started_at, id);
CREATE INDEX IF NOT EXISTS agent_run_message ON agent_run(user_message_id);
CREATE INDEX IF NOT EXISTS conversation_recent ON conversation(workspace_id, updated_at DESC, id);
CREATE INDEX IF NOT EXISTS knowledge_by_source ON knowledge_document(datasource_id, updated_at DESC, id);
"""), (5, """
CREATE TABLE IF NOT EXISTS deleted_conversation (
    conversation_id TEXT PRIMARY KEY REFERENCES conversation(id),
    deleted_at TEXT NOT NULL
);
"""), (6, """
CREATE TABLE IF NOT EXISTS organization_identity (
    issuer TEXT NOT NULL, subject TEXT NOT NULL,
    user_id TEXT NOT NULL UNIQUE REFERENCES app_user(id),
    PRIMARY KEY (issuer, subject)
);
"""))


def migrate(path):
    """Create or upgrade workspace.sqlite. A second call does not rebuild tables."""
    path = Path(path)
    if path.exists():
        _reject_foreign(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(path)) as conn:
        # executescript implicitly commits under legacy transaction control.
        conn.autocommit = True
        conn.execute('BEGIN IMMEDIATE')
        conn.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations ('
            'version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        current = conn.execute('SELECT COALESCE(MAX(version), 0) FROM schema_migrations').fetchone()[0]
        if current > SCHEMA_VERSION:
            raise ValueError('Application database is newer than this code')
        if current < 3 and not fts5_available(conn):
            raise ValueError(
                'This SQLite build does not provide FTS5, so the knowledge index cannot be created')
        for version, script in _MIGRATIONS:
            if version <= current:
                continue
            conn.executescript(script)
            conn.execute(
                'INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)',
                (version, utc_now()))
        conn.execute('COMMIT')
    return path


def sync_registered_sources(path, catalog):
    """Copy public SourceRegistry records into the application database.

    `catalog` must already be the path-free public list. This function does not
    open a user database and does not store filesystem paths.
    """
    if not isinstance(catalog, list):
        raise ValueError('Expected a list of public data sources')
    store = WorkspaceStore(path)
    created_at = utc_now()
    store.ensure_local_workspace(created_at)
    store.ensure_local_user(created_at)
    for entry in catalog:
        if not isinstance(entry, dict):
            raise ValueError('Data source metadata cannot include server paths or credentials')
        source = data_source_from_catalog(
            LOCAL_WORKSPACE_ID, entry, secret_ref='sources:' + entry.get('id', ''),
            created_at=created_at)
        store.register_data_source(source)
    return store


def record_saved_run(store, *, run_id, manifest, task, result, artifact_dir, conversation_id=None):
    """Project one saved run directory into the workspace database.

    The directory must already contain the task. Only the question text is
    stored on the user message. Reference SQL, samples, and trace stay out.
    `conversation_id` appends to that conversation instead of creating one.
    """
    if not isinstance(manifest, dict) or not isinstance(task, dict):
        raise ValueError('Saved run is missing manifest or task')
    question = manifest.get('question')
    if not isinstance(question, dict):
        raise ValueError('Saved run is missing a question')
    text = question.get('question')
    datasource_id = question.get('db_id')
    if not isinstance(text, str) or not isinstance(datasource_id, str):
        raise ValueError('Saved run is missing a question')
    created_at = manifest.get('created_at') or task.get('started_at')
    body = text.strip()
    try:
        existing = store.get_agent_run(run_id)
    except ValueError as error:
        if str(error) != 'Agent run is not registered':
            raise
        existing = None
    projected = _run_from_saved(
        run_id, datasource_id, manifest, task, result, artifact_dir,
        conversation_id=None if existing is None else existing.conversation_id,
        user_message_id=None if existing is None else existing.user_message_id)
    if existing is None:
        store.ensure_local_user(created_at)
    if existing is None and conversation_id:
        projected = replace(projected, conversation_id=conversation_id, user_message_id=None)
        return store.attach_run(
            conversation_id=conversation_id, body=body, created_at=created_at, run=projected)
    if existing is None:
        conversation = Conversation(
            str(uuid.uuid4()), LOCAL_WORKSPACE_ID, LOCAL_USER_ID, datasource_id,
            _question_title(body), created_at, created_at)
        message = Message(str(uuid.uuid4()), 1, 'user', body, created_at, conversation.id)
        projected = replace(
            projected, conversation_id=conversation.id, user_message_id=message.id)
        return store.begin_run(conversation, message, projected)
    if conversation_id and existing.conversation_id != conversation_id:
        raise ValueError('Agent run belongs to another conversation')
    return store.save_agent_run(projected)


def _question_title(text):
    compact = ' '.join(text.split())
    if not compact:
        raise ValueError('Saved run is missing a question')
    if len(compact) <= 80:
        return compact
    return compact[:79] + '…'


def _run_from_saved(run_id, datasource_id, manifest, task, result, artifact_dir,
                    *, conversation_id, user_message_id):
    status = None
    score = None
    if isinstance(result, dict):
        status = result.get('status') or None
        if 'score' in result:
            score = result.get('score')
    review = manifest.get('semantic_review', False)
    return AgentRun(
        id=run_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        datasource_id=datasource_id,
        # The durable task file carries queued state. Keep compatibility with
        # existing workspace SQLite CHECK constraints when mirroring the task.
        state='running' if task.get('state') == 'queued' else task.get('state'),
        stage=task.get('stage') or None,
        started_at=task.get('started_at'),
        updated_at=task.get('updated_at'),
        ended_at=task.get('ended_at'),
        stop_requested_at=task.get('stop_requested_at'),
        error=task.get('error') or None,
        result_status=status,
        score=score,
        semantic_review=review if isinstance(review, bool) else False,
        artifact_dir=artifact_dir)


def _insert_agent_run(conn, run):
    conn.execute(
        'INSERT INTO agent_run(id, conversation_id, user_message_id, datasource_id, state, '
        'stage, started_at, updated_at, ended_at, stop_requested_at, error, result_status, '
        'score, semantic_review, artifact_dir) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        _run_values(run))


def _update_agent_run(conn, run):
    conn.execute(
        'UPDATE agent_run SET state = ?, stage = ?, started_at = ?, updated_at = ?, ended_at = ?, '
        'stop_requested_at = ?, error = ?, result_status = ?, score = ?, semantic_review = ? '
        'WHERE id = ?',
        (run.state, run.stage, run.started_at, run.updated_at, run.ended_at, run.stop_requested_at,
         run.error, run.result_status, run.score, int(run.semantic_review), run.id))


def _run_values(run):
    return (run.id, run.conversation_id, run.user_message_id, run.datasource_id, run.state,
            run.stage, run.started_at, run.updated_at, run.ended_at, run.stop_requested_at,
            run.error, run.result_status, run.score, int(run.semantic_review), run.artifact_dir)


def _conversation_from_row(row):
    return Conversation(
        id=row['id'], workspace_id=row['workspace_id'], user_id=row['user_id'],
        datasource_id=row['datasource_id'], title=row['title'],
        created_at=row['created_at'], updated_at=row['updated_at'])


def _run_from_row(row):
    return AgentRun(
        id=row['id'], datasource_id=row['datasource_id'], state=row['state'],
        semantic_review=bool(row['semantic_review']), conversation_id=row['conversation_id'],
        user_message_id=row['user_message_id'], stage=row['stage'], started_at=row['started_at'],
        updated_at=row['updated_at'], ended_at=row['ended_at'],
        stop_requested_at=row['stop_requested_at'], error=row['error'],
        result_status=row['result_status'], score=row['score'], artifact_dir=row['artifact_dir'])


def remove_database(path):
    """Delete an application database created by migrate. User databases are refused."""
    path = Path(path)
    if not path.exists():
        return
    _reject_foreign(path)
    for candidate in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
        if candidate.is_file():
            candidate.unlink()


class WorkspaceStore:
    """Conversation, message, and data-source metadata. Idempotent to reopen."""

    def __init__(self, path):
        self.path = migrate(path)

    def add_workspace(self, workspace):
        _expect(workspace, Workspace)
        with self._session() as conn:
            conn.execute(
                'INSERT INTO workspace(id, name, created_at, local_only) VALUES (?, ?, ?, 1)',
                (workspace.id, workspace.name, workspace.created_at))

    def add_user(self, user):
        _expect(user, User)
        with self._session() as conn:
            conn.execute(
                'INSERT INTO app_user(id, display_name, created_at) VALUES (?, ?, ?)',
                (user.id, user.display_name, user.created_at))

    def add_data_source(self, source):
        _expect(source, DataSource)
        with self._session() as conn:
            _require_workspace(conn, source.workspace_id)
            conn.execute(
                'INSERT INTO data_source(id, workspace_id, name, kind, read_only, status, '
                'secret_ref, schema_fingerprint, created_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)',
                (source.id, source.workspace_id, source.name, source.kind, source.status,
                 source.secret_ref, source.schema_fingerprint, source.created_at))

    def ensure_local_workspace(self, created_at):
        with self._session() as conn:
            row = conn.execute(
                'SELECT id, name, created_at, local_only FROM workspace WHERE id = ?',
                (LOCAL_WORKSPACE_ID,)).fetchone()
            if row is not None:
                return Workspace(row['id'], row['name'], row['created_at'], bool(row['local_only']))
            workspace = Workspace(LOCAL_WORKSPACE_ID, LOCAL_WORKSPACE_NAME, created_at)
            conn.execute(
                'INSERT INTO workspace(id, name, created_at, local_only) VALUES (?, ?, ?, 1)',
                (workspace.id, workspace.name, workspace.created_at))
            return workspace

    def ensure_local_user(self, created_at):
        """Create the single local owner. A restart keeps the same user id."""
        with self._session() as conn:
            _require_workspace(conn, LOCAL_WORKSPACE_ID)
            row = conn.execute(
                'SELECT id, display_name, created_at FROM app_user WHERE id = ?',
                (LOCAL_USER_ID,)).fetchone()
            if row is None:
                user = User(LOCAL_USER_ID, LOCAL_USER_NAME, created_at)
                conn.execute(
                    'INSERT INTO app_user(id, display_name, created_at) VALUES (?, ?, ?)',
                    (user.id, user.display_name, user.created_at))
            else:
                user = User(row['id'], row['display_name'], row['created_at'])
            if conn.execute(
                    'SELECT 1 FROM membership WHERE workspace_id = ? AND user_id = ?',
                    (LOCAL_WORKSPACE_ID, LOCAL_USER_ID)).fetchone() is None:
                conn.execute(
                    'INSERT INTO membership(workspace_id, user_id, role) VALUES (?, ?, ?)',
                    (LOCAL_WORKSPACE_ID, LOCAL_USER_ID, 'owner'))
            return user

    def register_data_source(self, source):
        """Insert a public data-source record, or keep the existing row on restart."""
        _expect(source, DataSource)
        with self._session() as conn:
            _require_workspace(conn, source.workspace_id)
            row = conn.execute(
                'SELECT workspace_id, name, kind, status, secret_ref, schema_fingerprint, created_at '
                'FROM data_source WHERE id = ?', (source.id,)).fetchone()
            if row is None:
                conn.execute(
                    'INSERT INTO data_source(id, workspace_id, name, kind, read_only, status, '
                    'secret_ref, schema_fingerprint, created_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)',
                    (source.id, source.workspace_id, source.name, source.kind, source.status,
                     source.secret_ref, source.schema_fingerprint, source.created_at))
                return source
            if row['workspace_id'] != source.workspace_id:
                raise ValueError('Data source belongs to another workspace')
            current = (row['name'], row['kind'], row['status'], row['secret_ref'], row['schema_fingerprint'])
            incoming = (source.name, source.kind, source.status, source.secret_ref, source.schema_fingerprint)
            if current != incoming:
                conn.execute(
                    'UPDATE data_source SET name = ?, kind = ?, status = ?, secret_ref = ?, '
                    'schema_fingerprint = ? WHERE id = ?',
                    (*incoming, source.id))
            return _source_from_row(conn.execute(
                'SELECT * FROM data_source WHERE id = ?', (source.id,)).fetchone())

    def get_data_source(self, source_id):
        with self._session() as conn:
            row = conn.execute('SELECT * FROM data_source WHERE id = ?', (source_id,)).fetchone()
        if row is None:
            raise ValueError('Data source is not registered')
        return _source_from_row(row)

    def discard_unpublished_source(self, source_id):
        """Roll back a newly inserted upload. Foreign keys protect referenced sources."""
        with self._session() as conn:
            conn.execute('DELETE FROM data_source WHERE id = ? AND workspace_id = ?',
                         (source_id, LOCAL_WORKSPACE_ID))

    def list_data_sources(self, workspace_id):
        with self._session() as conn:
            rows = conn.execute(
                'SELECT * FROM data_source WHERE workspace_id = ? ORDER BY id', (workspace_id,))
            return [_source_from_row(row) for row in rows]

    def begin_run(self, conversation, message, run):
        """Insert one conversation, its user message, and the agent run.

        The same run id updates that run instead of inserting another record.
        """
        _expect(conversation, Conversation)
        _expect(message, Message)
        _expect(run, AgentRun)
        if message.conversation_id != conversation.id or message.role != 'user' or message.seq != 1:
            raise ValueError('A run starts with one user message')
        if run.conversation_id != conversation.id or run.user_message_id != message.id:
            raise ValueError('Agent run is not linked to its conversation')
        if run.datasource_id != conversation.datasource_id:
            raise ValueError('Conversation data source does not match the run')
        if conversation.user_id != LOCAL_USER_ID or conversation.workspace_id != LOCAL_WORKSPACE_ID:
            raise ValueError('Runs belong to the local workspace user')
        with self._session() as conn:
            existing = conn.execute('SELECT id FROM agent_run WHERE id = ?', (run.id,)).fetchone()
            if existing is not None:
                _update_agent_run(conn, run)
                return _run_from_row(conn.execute(
                    'SELECT * FROM agent_run WHERE id = ?', (run.id,)).fetchone())
            _require_workspace(conn, conversation.workspace_id)
            if conn.execute('SELECT 1 FROM app_user WHERE id = ?', (conversation.user_id,)).fetchone() is None:
                raise ValueError('Conversation user is not registered')
            source = conn.execute(
                'SELECT workspace_id FROM data_source WHERE id = ?',
                (conversation.datasource_id,)).fetchone()
            if source is None or source['workspace_id'] != conversation.workspace_id:
                raise ValueError('Conversation data source is not registered')
            conn.execute(
                'INSERT INTO conversation(id, workspace_id, user_id, datasource_id, title, '
                'created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (conversation.id, conversation.workspace_id, conversation.user_id,
                 conversation.datasource_id, conversation.title, conversation.created_at,
                 conversation.updated_at))
            conn.execute(
                'INSERT INTO message(id, conversation_id, seq, role, body, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (message.id, message.conversation_id, message.seq, message.role,
                 message.body, message.created_at))
            _insert_agent_run(conn, run)
            return run

    def save_agent_run(self, run):
        """Update a run that was already linked. Does not create a conversation."""
        _expect(run, AgentRun)
        with self._session() as conn:
            existing = conn.execute(
                'SELECT conversation_id, user_message_id FROM agent_run WHERE id = ?',
                (run.id,)).fetchone()
            if existing is None:
                raise ValueError('Agent run is not registered')
            if existing['conversation_id'] != run.conversation_id or existing['user_message_id'] != run.user_message_id:
                raise ValueError('Agent run link cannot be replaced')
            _update_agent_run(conn, run)
            return _run_from_row(conn.execute(
                'SELECT * FROM agent_run WHERE id = ?', (run.id,)).fetchone())

    def get_agent_run(self, run_id):
        with self._session() as conn:
            row = conn.execute('SELECT * FROM agent_run WHERE id = ?', (run_id,)).fetchone()
        if row is None:
            raise ValueError('Agent run is not registered')
        return _run_from_row(row)

    def list_agent_runs(self, conversation_id=None):
        with self._session() as conn:
            if conversation_id is None:
                rows = conn.execute('SELECT * FROM agent_run ORDER BY started_at, id')
            else:
                rows = conn.execute(
                    'SELECT r.* FROM agent_run r LEFT JOIN message m ON m.id = r.user_message_id '
                    'WHERE r.conversation_id = ? ORDER BY m.seq, r.started_at, r.id',
                    (conversation_id,))
            return [_run_from_row(row) for row in rows]

    def recent_message_runs(self, conversation_id, *, exclude_run_id, limit):
        """Return joined turns in message order without loading an entire transcript."""
        with self._session() as conn:
            rows = conn.execute(
                'SELECT r.*, m.seq, m.role, m.body, m.created_at AS message_created_at '
                'FROM message m JOIN agent_run r ON r.user_message_id = m.id '
                'AND r.conversation_id = m.conversation_id '
                'WHERE m.conversation_id = ? AND r.id != ? AND m.seq < COALESCE(('
                'SELECT prior.seq FROM agent_run current JOIN message prior ON prior.id = current.user_message_id '
                'WHERE current.id = ? AND prior.conversation_id = m.conversation_id'
                '), 9223372036854775807) ORDER BY m.seq DESC LIMIT ?',
                (conversation_id, exclude_run_id, exclude_run_id, limit)).fetchall()
        return [(Message(row['user_message_id'], row['seq'], row['role'], row['body'],
                         row['message_created_at'], conversation_id), _run_from_row(row))
                for row in reversed(rows)]

    def attach_run(self, *, conversation_id, body, created_at, run):
        """Append one user message and its run onto an existing conversation."""
        _expect(run, AgentRun)
        with self._session() as conn:
            existing = conn.execute(
                'SELECT conversation_id, user_message_id FROM agent_run WHERE id = ?',
                (run.id,)).fetchone()
            if existing is not None:
                if existing['conversation_id'] != conversation_id:
                    raise ValueError('Agent run belongs to another conversation')
                linked = replace(
                    run, conversation_id=existing['conversation_id'],
                    user_message_id=existing['user_message_id'])
                _update_agent_run(conn, linked)
                return _run_from_row(conn.execute(
                    'SELECT * FROM agent_run WHERE id = ?', (run.id,)).fetchone())
            conv = conn.execute(
                'SELECT workspace_id, datasource_id, updated_at FROM conversation WHERE id = ?',
                (conversation_id,)).fetchone()
            if conv is None:
                raise ValueError('Conversation is not registered')
            if conv['workspace_id'] != LOCAL_WORKSPACE_ID or conv['datasource_id'] != run.datasource_id:
                raise ValueError('Conversation data source does not match the run')
            seq = conn.execute(
                'SELECT COALESCE(MAX(seq), 0) + 1 FROM message WHERE conversation_id = ?',
                (conversation_id,)).fetchone()[0]
            message = Message(str(uuid.uuid4()), seq, 'user', body, created_at, conversation_id)
            conn.execute(
                'INSERT INTO message(id, conversation_id, seq, role, body, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (message.id, message.conversation_id, message.seq, message.role,
                 message.body, message.created_at))
            linked = replace(run, conversation_id=conversation_id, user_message_id=message.id)
            _insert_agent_run(conn, linked)
            if created_at >= conv['updated_at']:
                conn.execute(
                    'UPDATE conversation SET updated_at = ? WHERE id = ?',
                    (created_at, conversation_id))
            return linked

    def add_conversation(self, conversation):
        _expect(conversation, Conversation)
        with self._session() as conn:
            _require_workspace(conn, conversation.workspace_id)
            if conn.execute('SELECT 1 FROM app_user WHERE id = ?', (conversation.user_id,)).fetchone() is None:
                raise ValueError('Conversation user is not registered')
            row = conn.execute(
                'SELECT workspace_id FROM data_source WHERE id = ?',
                (conversation.datasource_id,)).fetchone()
            if row is None:
                raise ValueError('Conversation data source is not registered')
            if row['workspace_id'] != conversation.workspace_id:
                raise ValueError('Conversation data source belongs to another workspace')
            conn.execute(
                'INSERT INTO conversation(id, workspace_id, user_id, datasource_id, title, '
                'created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                (conversation.id, conversation.workspace_id, conversation.user_id,
                 conversation.datasource_id, conversation.title, conversation.created_at,
                 conversation.updated_at))

    def get_conversation(self, conversation_id):
        with self._session() as conn:
            row = conn.execute('SELECT * FROM conversation WHERE id = ? AND id NOT IN (SELECT conversation_id FROM deleted_conversation)', (conversation_id,)).fetchone()
        if row is None:
            raise ValueError('Conversation is not registered')
        return _conversation_from_row(row)

    def list_conversations(self, user_id=None):
        with self._session() as conn:
            rows = conn.execute(
                'SELECT * FROM conversation WHERE workspace_id = ? AND (? IS NULL OR user_id = ?) '
                'AND id NOT IN (SELECT conversation_id FROM deleted_conversation) ORDER BY updated_at DESC, id',
                (LOCAL_WORKSPACE_ID, user_id, user_id))
            return [_conversation_from_row(row) for row in rows]

    def register_team_user(self, user_id, name):
        with self._session() as conn:
            conn.execute('INSERT INTO app_user(id, display_name, created_at) VALUES (?, ?, ?) '
                         'ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name',
                         (user_id, name, utc_now()))
            conn.execute("INSERT OR IGNORE INTO membership(workspace_id, user_id, role) VALUES (?, ?, 'analyst')",
                         (LOCAL_WORKSPACE_ID, user_id))

    def register_organization_users(self, issuer, accounts):
        """Prevent silently transferring an existing account to a different identity."""
        with self._session() as conn:
            for subject, account in accounts.items():
                identity = conn.execute('SELECT issuer, subject FROM organization_identity WHERE user_id=?',
                                        (account.id,)).fetchone()
                if identity is not None and tuple(identity) != (issuer, subject):
                    raise ValueError('An account ID is already bound to another organization identity')
                binding = conn.execute('SELECT user_id FROM organization_identity WHERE issuer=? AND subject=?',
                                       (issuer, subject)).fetchone()
                if binding is not None and binding['user_id'] != account.id:
                    raise ValueError('An organization identity already uses another account ID')
                conn.execute('INSERT INTO app_user(id, display_name, created_at) VALUES (?, ?, ?) '
                             'ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name',
                             (account.id, account.name, utc_now()))
                conn.execute("INSERT OR IGNORE INTO membership(workspace_id, user_id, role) VALUES (?, ?, 'analyst')",
                             (LOCAL_WORKSPACE_ID, account.id))
                conn.execute('INSERT OR IGNORE INTO organization_identity(issuer, subject, user_id) VALUES (?, ?, ?)',
                             (issuer, subject, account.id))

    def has_organization_users(self):
        with self._session() as conn:
            return conn.execute('SELECT 1 FROM organization_identity LIMIT 1').fetchone() is not None

    def has_team_users(self):
        with self._session() as conn:
            return conn.execute('SELECT 1 FROM membership WHERE workspace_id=? AND user_id!=? LIMIT 1',
                                (LOCAL_WORKSPACE_ID, LOCAL_USER_ID)).fetchone() is not None

    @staticmethod
    def _audit(conn, user_id, action, object_type, object_id, outcome):
        conn.execute('INSERT INTO audit_event(id, at, actor_user_id, workspace_id, action, object_type, '
                     'object_id, outcome, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     (str(uuid.uuid4()), utc_now(), user_id, LOCAL_WORKSPACE_ID,
                      action, object_type, object_id, outcome, '{}'))

    def audit(self, user_id, action, object_type, object_id, outcome):
        with self._session() as conn:
            self._audit(conn, user_id, action, object_type, object_id, outcome)

    def hide_conversations(self, user_id, conversation_ids):
        with self._session() as conn:
            conn.executemany('INSERT INTO deleted_conversation(conversation_id, deleted_at) VALUES (?, ?)',
                             [(cid, utc_now()) for cid in conversation_ids])
            for cid in conversation_ids:
                self._audit(conn, user_id, 'delete', 'conversation', cid, 'ok')

    def deleted_run_ids(self):
        with self._session() as conn:
            return {row[0] for row in conn.execute('SELECT r.id FROM agent_run r JOIN deleted_conversation d '
                                                   'ON d.conversation_id=r.conversation_id')}

    def save_profile(self, datasource_id, workspace_id, fingerprint, extracted_at, profile_json):
        """Replace the stored catalog profile. This does not open a user database."""
        with self._session() as conn:
            row = conn.execute(
                'SELECT workspace_id FROM data_source WHERE id = ?', (datasource_id,)).fetchone()
            if row is None or row['workspace_id'] != workspace_id:
                raise ValueError('Data source is not registered')
            conn.execute(
                'INSERT INTO database_profile(datasource_id, workspace_id, fingerprint, '
                'extracted_at, profile_json) VALUES (?, ?, ?, ?, ?) '
                'ON CONFLICT(datasource_id) DO UPDATE SET fingerprint = excluded.fingerprint, '
                'extracted_at = excluded.extracted_at, profile_json = excluded.profile_json',
                (datasource_id, workspace_id, fingerprint, extracted_at, profile_json))

    def get_profile_record(self, datasource_id):
        with self._session() as conn:
            row = conn.execute(
                'SELECT fingerprint, extracted_at, profile_json FROM database_profile '
                'WHERE datasource_id = ?', (datasource_id,)).fetchone()
        if row is None:
            return None
        return {'fingerprint': row['fingerprint'], 'extracted_at': row['extracted_at'],
                'profile_json': row['profile_json']}

    def insert_knowledge(self, record, *, body=None):
        with self._session() as conn:
            _require_registered_source(conn, record['workspace_id'], record['datasource_id'])
            conn.execute(
                'INSERT INTO knowledge_document(id, workspace_id, datasource_id, title, format, '
                'content_hash, version, provenance, storage_key, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (record['id'], record['workspace_id'], record['datasource_id'], record['title'],
                 record['format'], record['content_hash'], record['version'], record['provenance'],
                 record['storage_key'], record['created_at'], record['updated_at']))
            if body is not None:
                upsert_index(conn, record, body)

    def update_knowledge(self, record, *, body=None):
        with self._session() as conn:
            changed = conn.execute(
                'UPDATE knowledge_document SET title = ?, format = ?, content_hash = ?, storage_key = ?, '
                'version = ?, updated_at = ? WHERE id = ? AND datasource_id = ? AND version = ?',
                (record['title'], record['format'], record['content_hash'], record['storage_key'],
                 record['version'], record['updated_at'], record['id'], record['datasource_id'],
                 record['version'] - 1))
            if changed.rowcount != 1:
                raise ValueError('Knowledge document version is stale')
            if body is not None:
                upsert_index(conn, record, body)

    def delete_knowledge(self, datasource_id, knowledge_id):
        with self._session() as conn:
            row = conn.execute(
                'SELECT storage_key FROM knowledge_document WHERE id = ? AND datasource_id = ?',
                (knowledge_id, datasource_id)).fetchone()
            if row is None:
                raise ValueError('Knowledge document is not registered')
            delete_index(conn, knowledge_id)
            conn.execute(
                'DELETE FROM knowledge_document WHERE id = ? AND datasource_id = ?',
                (knowledge_id, datasource_id))
            return row['storage_key']

    def list_all_knowledge_rows(self):
        with self._session() as conn:
            rows = conn.execute('SELECT * FROM knowledge_document ORDER BY datasource_id, id')
            return [dict(row) for row in rows]

    def knowledge_index_current(self):
        """True when every knowledge row has the same id, version, and hash in FTS."""
        with self._session() as conn:
            docs = conn.execute(
                'SELECT id, version, content_hash FROM knowledge_document').fetchall()
            indexed = conn.execute(
                'SELECT knowledge_id, version, content_hash FROM knowledge_fts').fetchall()
        left = {(row['id'], str(row['version']), row['content_hash']) for row in docs}
        right = {(row['knowledge_id'], str(row['version']), row['content_hash']) for row in indexed}
        return left == right

    def replace_knowledge_index(self, rows):
        with self._session() as conn:
            try:
                conn.execute('DELETE FROM knowledge_fts')
            except sqlite3.DatabaseError:
                conn.execute('DROP TABLE IF EXISTS knowledge_fts')
                conn.execute(FTS_CREATE_SQL)
            for row in rows:
                upsert_index(conn, row, row['body'])

    def search_knowledge(self, datasource_id, question, limit):
        with self._session() as conn:
            return search_index(conn, datasource_id, question, limit)

    def get_knowledge_row(self, datasource_id, knowledge_id):
        with self._session() as conn:
            row = conn.execute(
                'SELECT * FROM knowledge_document WHERE id = ? AND datasource_id = ?',
                (knowledge_id, datasource_id)).fetchone()
        if row is None:
            raise ValueError('Knowledge document is not registered')
        return dict(row)

    def list_knowledge_rows(self, datasource_id):
        with self._session() as conn:
            _require_registered_source(conn, LOCAL_WORKSPACE_ID, datasource_id)
            rows = conn.execute(
                'SELECT * FROM knowledge_document WHERE datasource_id = ? '
                'ORDER BY updated_at DESC, id', (datasource_id,))
            return [dict(row) for row in rows]

    def add_message(self, message):
        _expect(message, Message)
        if message.conversation_id is None:
            raise ValueError('Stored messages require a conversation')
        with self._session() as conn:
            row = conn.execute(
                'SELECT updated_at FROM conversation WHERE id = ?',
                (message.conversation_id,)).fetchone()
            if row is None:
                raise ValueError('Conversation is not registered')
            expected = conn.execute(
                'SELECT COALESCE(MAX(seq), 0) + 1 FROM message WHERE conversation_id = ?',
                (message.conversation_id,)).fetchone()[0]
            if message.seq != expected:
                raise ValueError(f'Message seq must be {expected}')
            conn.execute(
                'INSERT INTO message(id, conversation_id, seq, role, body, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (message.id, message.conversation_id, message.seq, message.role,
                 message.body, message.created_at))
            if message.created_at >= row['updated_at']:
                conn.execute(
                    'UPDATE conversation SET updated_at = ? WHERE id = ?',
                    (message.created_at, message.conversation_id))
        return message

    def list_messages(self, conversation_id):
        with self._session() as conn:
            rows = conn.execute(
                'SELECT * FROM message WHERE conversation_id = ? ORDER BY seq',
                (conversation_id,))
            return [Message(
                id=row['id'], conversation_id=row['conversation_id'], seq=row['seq'],
                role=row['role'], body=row['body'], created_at=row['created_at']) for row in rows]

    @contextmanager
    def _session(self):
        conn = _connect(self.path)
        try:
            yield conn
            conn.commit()
        except sqlite3.IntegrityError as error:
            conn.rollback()
            raise ValueError('Metadata constraint failed') from error
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _expect(value, kind):
    if not isinstance(value, kind):
        raise TypeError(f'Expected {kind.__name__}')


def _require_workspace(conn, workspace_id):
    if conn.execute('SELECT 1 FROM workspace WHERE id = ?', (workspace_id,)).fetchone() is None:
        raise ValueError('Workspace is not registered')


def _require_registered_source(conn, workspace_id, datasource_id):
    row = conn.execute(
        'SELECT workspace_id FROM data_source WHERE id = ?', (datasource_id,)).fetchone()
    if row is None or row['workspace_id'] != workspace_id:
        raise ValueError('Data source is not registered')


def _source_from_row(row):
    return DataSource(
        id=row['id'], workspace_id=row['workspace_id'], name=row['name'], kind=row['kind'],
        created_at=row['created_at'], read_only=bool(row['read_only']), status=row['status'],
        secret_ref=row['secret_ref'], schema_fingerprint=row['schema_fingerprint'])


def _reject_foreign(path):
    """Refuse to change a SQLite file that is not an application workspace."""
    path = Path(path)
    if path.is_dir():
        raise ValueError('Refusing to change a database that is not an application workspace')
    try:
        uri = path.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            rows = conn.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT GLOB 'sqlite_*'")
            names = {row[0] for row in rows}
    except sqlite3.Error as error:
        raise ValueError('Refusing to change a database that is not an application workspace') from error
    # An existing SQLite file with no application migration is a user database,
    # even when it does not yet contain tables.
    if not names or not names <= APP_TABLES or 'schema_migrations' not in names:
        raise ValueError('Refusing to change a database that is not an application workspace')


def column_names(path):
    """Return every application column name. Used to prove secrets have no column."""
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute('PRAGMA query_only=ON')
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT GLOB 'sqlite_*'")]
        found = set()
        for table in tables:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', table):
                raise ValueError('Unexpected table in the application database')
            for info in conn.execute(f'PRAGMA table_info({table})'):
                found.add(info[1])
        return found
