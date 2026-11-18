"""SQLite metadata for one local workspace.

User databases are never opened here. Callers pass the application file path.
`live_ui.main` does not call this module.
"""
from contextlib import closing, contextmanager
from pathlib import Path
import re
import sqlite3

from sql_agent_lab.macsql.task_lifecycle import utc_now

from .models import (
    Conversation, DataSource, Message, User, Workspace, data_source_from_catalog,
)


SCHEMA_VERSION = 1
# One implicit local workspace. Restarts reuse this id instead of inserting another.
LOCAL_WORKSPACE_ID = '5c6a1e0a-7b34-4d5e-9f10-0a1b2c3d4e5f'
LOCAL_WORKSPACE_NAME = 'Local'
APP_TABLES = frozenset({
    'schema_migrations', 'workspace', 'app_user', 'membership', 'data_source',
    'data_source_grant', 'conversation', 'message', 'agent_run', 'audit_event',
})
# Columns that would copy a user database, a credential, or gold SQL.
FORBIDDEN_COLUMNS = frozenset({
    'path', 'password', 'passwd', 'secret', 'credential', 'sql', 'gold_sql',
    'reference_sql', 'host', 'port', 'filename', 'dsn',
})

_MIGRATIONS = ((SCHEMA_VERSION, """
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
"""),)


def migrate(path):
    """Create or upgrade workspace.sqlite. A second call does not rebuild tables."""
    path = Path(path)
    if path.exists():
        _reject_foreign(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(path)) as conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations ('
            'version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)')
        current = conn.execute('SELECT COALESCE(MAX(version), 0) FROM schema_migrations').fetchone()[0]
        if current > SCHEMA_VERSION:
            raise ValueError('Application database is newer than this code')
        for version, script in _MIGRATIONS:
            if version <= current:
                continue
            conn.executescript(script)
            conn.execute(
                'INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)',
                (version, utc_now()))
        conn.commit()
    return path


def sync_registered_sources(path, catalog):
    """Copy public SourceRegistry records into the application database.

    `catalog` must already be the path-free public list. This function does not
    open a user database and does not store filesystem paths.
    """
    if not isinstance(catalog, list) or not catalog:
        raise ValueError('At least one public data source is required')
    store = WorkspaceStore(path)
    created_at = utc_now()
    store.ensure_local_workspace(created_at)
    for entry in catalog:
        if not isinstance(entry, dict):
            raise ValueError('Data source metadata cannot include server paths or credentials')
        source = data_source_from_catalog(
            LOCAL_WORKSPACE_ID, entry, secret_ref='sources:' + entry.get('id', ''),
            created_at=created_at)
        store.register_data_source(source)
    return store


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

    def list_data_sources(self, workspace_id):
        with self._session() as conn:
            rows = conn.execute(
                'SELECT * FROM data_source WHERE workspace_id = ? ORDER BY id', (workspace_id,))
            return [_source_from_row(row) for row in rows]

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
            row = conn.execute('SELECT * FROM conversation WHERE id = ?', (conversation_id,)).fetchone()
        if row is None:
            raise ValueError('Conversation is not registered')
        return Conversation(
            id=row['id'], workspace_id=row['workspace_id'], user_id=row['user_id'],
            datasource_id=row['datasource_id'], title=row['title'],
            created_at=row['created_at'], updated_at=row['updated_at'])

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
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
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
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        found = set()
        for table in tables:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', table):
                raise ValueError('Unexpected table in the application database')
            for info in conn.execute(f'PRAGMA table_info({table})'):
                found.add(info[1])
        return found
