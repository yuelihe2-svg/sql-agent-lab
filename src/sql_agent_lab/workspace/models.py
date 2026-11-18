"""Frozen workspace metadata. Public JSON never carries server paths or secrets."""
from dataclasses import dataclass, fields
from datetime import datetime
import json
import re
import uuid


DATA_SOURCE_ID = re.compile(r'[A-Za-z0-9_-]{1,100}')
SECRET_REF = re.compile(r'[A-Za-z0-9_:-]{1,120}')
ROLES = frozenset({'owner', 'analyst', 'developer'})
SOURCE_KINDS = frozenset({'sqlite', 'postgres', 'mysql'})
SOURCE_STATUSES = frozenset({'ready', 'unavailable'})
MESSAGE_ROLES = frozenset({'user', 'assistant', 'tool_summary'})
RUN_STATES = frozenset({
    'running', 'stopping', 'completed', 'failed', 'stopped', 'interrupted', 'unknown'})
RESULT_STATUSES = frozenset({
    'completed', 'needs_review', 'failed', 'evaluation_failed', 'stopped'})
AUDIT_OUTCOMES = frozenset({'ok', 'denied'})
# Names only. Suggested actions are documented; callers may add a dotted name later.
AUDIT_ACTION = re.compile(r'[a-z][a-z0-9_.]{0,63}')

# Keys that would carry a filesystem path, credential, prompt, or reference SQL.
FORBIDDEN_JSON_KEYS = frozenset({
    'path', 'paths', 'password', 'passwd', 'pwd', 'secret', 'secrets',
    'credential', 'credentials', 'token', 'sql', 'gold', 'gold_sql',
    'reference_sql', 'prompt', 'prompts', 'rows', 'host', 'port',
    'filename', 'artifact_dir', 'connection_string', 'dsn',
})
CATALOG_FIELDS = frozenset({'id', 'name', 'kind', 'read_only'})


def _reject_forbidden_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_JSON_KEYS:
                raise ValueError(f'Field {key} cannot be stored in workspace metadata')
            _reject_forbidden_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_keys(item)


def _uuid(value, field, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a UUID')
    try:
        uuid.UUID(value)
    except ValueError as error:
        raise ValueError(f'{field} must be a UUID') from error
    return value


def _timestamp(value, field, *, required=True):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f'{field} must be an ISO-8601 timestamp')
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f'{field} must be an ISO-8601 timestamp') from error
    if parsed.tzinfo is None:
        raise ValueError(f'{field} must include a timezone')
    return value


def _text(value, field, *, limit):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f'{field} is required')
    if len(value) > limit:
        raise ValueError(f'{field} is too long')
    return value


def _choice(value, field, allowed):
    if value not in allowed:
        raise ValueError(f'{field} is not an allowed value')
    return value


def _source_id(value):
    if not isinstance(value, str) or not DATA_SOURCE_ID.fullmatch(value):
        raise ValueError('Data source ID must use 1–100 letters, numbers, underscores or hyphens')
    return value


def _secret_ref(value):
    if value is None:
        return None
    if not isinstance(value, str) or not SECRET_REF.fullmatch(value):
        raise ValueError('secret_ref must name an external config entry')
    if 'password' in value.lower():
        raise ValueError('secret_ref must name an external config entry')
    return value


def _score(value):
    if value is None or value == '':
        return None
    if value in (0, 1, 0.0, 1.0) and not isinstance(value, bool):
        return int(value)
    raise ValueError('score must be 0, 1, or empty')


class JsonModel:
    """JSON is the public metadata form. Server paths stay on the object only."""

    HIDDEN_JSON_FIELDS = frozenset()

    def to_public_dict(self):
        payload = {}
        for item in fields(self):
            if item.name in self.HIDDEN_JSON_FIELDS:
                continue
            payload[item.name] = getattr(self, item.name)
        _reject_forbidden_keys(payload)
        return payload

    def to_json(self):
        return json.dumps(self.to_public_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, text):
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError('Expected a JSON object')
        _reject_forbidden_keys(payload)
        known = {item.name for item in fields(cls)} - cls.HIDDEN_JSON_FIELDS
        extra = set(payload) - known
        if extra:
            raise ValueError('Unexpected fields: ' + ', '.join(sorted(extra)))
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class Workspace(JsonModel):
    id: str
    name: str
    created_at: str
    local_only: bool = True

    def __post_init__(self):
        _uuid(self.id, 'id')
        _text(self.name, 'name', limit=200)
        _timestamp(self.created_at, 'created_at')
        if self.local_only is not True:
            raise ValueError('local_only must be true')


@dataclass(frozen=True, slots=True)
class User(JsonModel):
    id: str
    display_name: str
    created_at: str

    def __post_init__(self):
        _uuid(self.id, 'id')
        _text(self.display_name, 'display_name', limit=200)
        _timestamp(self.created_at, 'created_at')


@dataclass(frozen=True, slots=True)
class Membership(JsonModel):
    workspace_id: str
    user_id: str
    role: str

    def __post_init__(self):
        _uuid(self.workspace_id, 'workspace_id')
        _uuid(self.user_id, 'user_id')
        _choice(self.role, 'role', ROLES)


@dataclass(frozen=True, slots=True)
class DataSource(JsonModel):
    id: str
    workspace_id: str
    name: str
    kind: str
    created_at: str
    read_only: bool = True
    status: str = 'ready'
    secret_ref: str | None = None
    schema_fingerprint: str | None = None

    def __post_init__(self):
        _source_id(self.id)
        _uuid(self.workspace_id, 'workspace_id')
        _text(self.name, 'name', limit=200)
        _choice(self.kind, 'kind', SOURCE_KINDS)
        _timestamp(self.created_at, 'created_at')
        if self.read_only is not True:
            raise ValueError('read_only must be true')
        _choice(self.status, 'status', SOURCE_STATUSES)
        _secret_ref(self.secret_ref)
        if self.schema_fingerprint is not None:
            if (not isinstance(self.schema_fingerprint, str)
                    or not re.fullmatch(r'[A-Fa-f0-9]{1,128}', self.schema_fingerprint)):
                raise ValueError('schema_fingerprint must be a hex digest')

    def catalog(self):
        # Same public shape as SQLiteSource.public(): no path and no secret.
        return {'id': self.id, 'name': self.name, 'kind': self.kind, 'read_only': True}


def data_source_from_catalog(workspace_id, catalog, *, secret_ref, created_at,
                             status='ready', schema_fingerprint=None):
    """Bind a data-source ID from the public catalog. Paths are not accepted."""
    if not isinstance(catalog, dict) or set(catalog) - CATALOG_FIELDS:
        raise ValueError('Data source metadata cannot include server paths or credentials')
    return DataSource(
        id=catalog['id'], workspace_id=workspace_id, name=catalog['name'],
        kind=catalog['kind'], created_at=created_at, read_only=catalog['read_only'],
        status=status, secret_ref=secret_ref, schema_fingerprint=schema_fingerprint)


@dataclass(frozen=True, slots=True)
class DataSourceGrant(JsonModel):
    user_id: str
    datasource_id: str
    can_query: bool

    def __post_init__(self):
        _uuid(self.user_id, 'user_id')
        _source_id(self.datasource_id)
        if not isinstance(self.can_query, bool):
            raise ValueError('can_query must be a boolean')


@dataclass(frozen=True, slots=True)
class Conversation(JsonModel):
    id: str
    workspace_id: str
    user_id: str
    datasource_id: str
    title: str
    created_at: str
    updated_at: str

    def __post_init__(self):
        _uuid(self.id, 'id')
        _uuid(self.workspace_id, 'workspace_id')
        _uuid(self.user_id, 'user_id')
        _source_id(self.datasource_id)
        _text(self.title, 'title', limit=200)
        _timestamp(self.created_at, 'created_at')
        _timestamp(self.updated_at, 'updated_at')


@dataclass(frozen=True, slots=True)
class Message(JsonModel):
    id: str
    seq: int
    role: str
    body: str
    created_at: str
    conversation_id: str | None = None

    def __post_init__(self):
        _uuid(self.id, 'id')
        _uuid(self.conversation_id, 'conversation_id', required=False)
        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 1:
            raise ValueError('seq must start at 1')
        _choice(self.role, 'role', MESSAGE_ROLES)
        _text(self.body, 'body', limit=8000)
        _timestamp(self.created_at, 'created_at')


@dataclass(frozen=True, slots=True)
class AgentRun(JsonModel):
    id: str
    datasource_id: str
    state: str
    semantic_review: bool = False
    conversation_id: str | None = None
    user_message_id: str | None = None
    stage: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    ended_at: str | None = None
    stop_requested_at: str | None = None
    error: str | None = None
    result_status: str | None = None
    score: int | None = None
    artifact_dir: str | None = None

    HIDDEN_JSON_FIELDS = frozenset({'artifact_dir'})

    def __post_init__(self):
        if (not isinstance(self.id, str) or not self.id or self.id in ('.', '..')
                or len(self.id) > 200 or any(sep in self.id for sep in ('/', '\\', '\x00'))):
            raise ValueError('Run id cannot be a path')
        _source_id(self.datasource_id)
        _choice(self.state, 'state', RUN_STATES)
        if not isinstance(self.semantic_review, bool):
            raise ValueError('semantic_review must be a boolean')
        _uuid(self.conversation_id, 'conversation_id', required=False)
        _uuid(self.user_message_id, 'user_message_id', required=False)
        if self.stage is not None:
            _text(self.stage, 'stage', limit=100)
        for name in ('started_at', 'updated_at', 'ended_at', 'stop_requested_at'):
            _timestamp(getattr(self, name), name, required=False)
        if self.error is not None:
            _text(self.error, 'error', limit=2000)
        if self.result_status is not None:
            _choice(self.result_status, 'result_status', RESULT_STATUSES)
        object.__setattr__(self, 'score', _score(self.score))
        if self.artifact_dir is not None and not isinstance(self.artifact_dir, str):
            raise ValueError('artifact_dir must be a server path')


@dataclass(frozen=True, slots=True)
class AuditEvent(JsonModel):
    id: str
    at: str
    action: str
    object_type: str
    object_id: str
    outcome: str
    detail: dict
    actor_user_id: str | None = None
    workspace_id: str | None = None

    def __post_init__(self):
        _uuid(self.id, 'id')
        _timestamp(self.at, 'at')
        _uuid(self.actor_user_id, 'actor_user_id', required=False)
        _uuid(self.workspace_id, 'workspace_id', required=False)
        if not isinstance(self.action, str) or not AUDIT_ACTION.fullmatch(self.action):
            raise ValueError('action is not an allowed value')
        _text(self.object_type, 'object_type', limit=100)
        _text(self.object_id, 'object_id', limit=200)
        if any(sep in self.object_id for sep in ('/', '\\')):
            raise ValueError('object_id cannot be a path')
        _choice(self.outcome, 'outcome', AUDIT_OUTCOMES)
        if not isinstance(self.detail, dict):
            raise ValueError('detail must be a short JSON object')
        _reject_forbidden_keys(self.detail)
        encoded = json.dumps(self.detail, ensure_ascii=False)
        if len(encoded) > 1024:
            raise ValueError('detail is too long')
        object.__setattr__(self, 'detail', json.loads(encoded))


def public_round_trip(model):
    """Return the model restored from its public JSON."""
    return type(model).from_json(model.to_json())
