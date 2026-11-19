"""Optional user-written knowledge for one registered data source.

Documents are user claims. They are not checked against the business database
and are not inserted into the SQL Agent prompt.
"""
from pathlib import Path
import hashlib
import json
import re
import uuid

from sql_agent_lab.macsql.task_lifecycle import utc_now

from .database import LOCAL_WORKSPACE_ID


FORMATS = frozenset({'markdown', 'text', 'json'})
BODY_LIMIT = 24_000
TITLE_LIMIT = 200
_KEY = re.compile(r'[0-9a-f]{32}')
PUBLIC_FIELDS = (
    'id', 'datasource_id', 'title', 'format', 'content_hash', 'version',
    'provenance', 'created_at', 'updated_at', 'body', 'verification',
)


def knowledge_dir(workspace_db):
    """Directory beside the workspace file. Clients never choose this path."""
    database = Path(workspace_db).resolve()
    root = database.parent / (database.stem + '-knowledge')
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_knowledge(store, workspace_db, datasource_id, title, document_format, body):
    title, document_format, body = _validate(title, document_format, body)
    store.get_data_source(datasource_id)
    now = utc_now()
    key = uuid.uuid4().hex
    record = {
        'id': str(uuid.uuid4()),
        'workspace_id': LOCAL_WORKSPACE_ID,
        'datasource_id': datasource_id,
        'title': title,
        'format': document_format,
        'content_hash': _hash(body),
        'version': 1,
        'provenance': 'user',
        'storage_key': key,
        'created_at': now,
        'updated_at': now,
    }
    directory = knowledge_dir(workspace_db)
    _write_temp(directory, key, body)
    try:
        store.insert_knowledge(record)
    except Exception:
        _discard_temp(directory, key)
        raise
    try:
        _commit_temp(directory, key)
    except OSError:
        store.delete_knowledge(datasource_id, record['id'])
        _discard_temp(directory, key)
        raise
    return _public(record, body)


def update_knowledge(store, workspace_db, datasource_id, knowledge_id, title, document_format, body):
    title, document_format, body = _validate(title, document_format, body)
    current = store.get_knowledge_row(datasource_id, knowledge_id)
    directory = knowledge_dir(workspace_db)
    key = current['storage_key']
    _require_key(key)
    updated = {
        'id': current['id'],
        'datasource_id': datasource_id,
        'title': title,
        'format': document_format,
        'content_hash': _hash(body),
        'version': current['version'] + 1,
        'updated_at': utc_now(),
    }
    _write_temp(directory, key, body)
    try:
        store.update_knowledge(updated)
    except Exception:
        _discard_temp(directory, key)
        raise
    try:
        _commit_temp(directory, key)
    except OSError:
        _discard_temp(directory, key)
        raise ValueError('Knowledge document was not saved')
    current.update(updated)
    return _public(current, body)


def delete_knowledge(store, workspace_db, datasource_id, knowledge_id):
    key = store.delete_knowledge(datasource_id, knowledge_id)
    path = _document_path(knowledge_dir(workspace_db), key)
    if path.is_file():
        path.unlink()
    return {'id': knowledge_id, 'datasource_id': datasource_id, 'deleted': True}


def read_knowledge(store, workspace_db, datasource_id, knowledge_id):
    row = store.get_knowledge_row(datasource_id, knowledge_id)
    body = _read_body(knowledge_dir(workspace_db), row)
    return _public(row, body)


def list_knowledge(store, workspace_db, datasource_id):
    documents = []
    directory = knowledge_dir(workspace_db)
    for row in store.list_knowledge_rows(datasource_id):
        documents.append(_public(row, _read_body(directory, row)))
    return documents


def _validate(title, document_format, body):
    if not isinstance(title, str) or not title.strip() or title != title.strip():
        raise ValueError('title is required')
    if len(title) > TITLE_LIMIT:
        raise ValueError('title is too long')
    if document_format not in FORMATS:
        raise ValueError('format is not supported')
    if not isinstance(body, str) or not body.strip():
        raise ValueError('body is required')
    if len(body) > BODY_LIMIT:
        raise ValueError('body is too long')
    if document_format == 'json':
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as error:
            raise ValueError('json knowledge must be valid JSON') from error
        if not isinstance(parsed, (dict, list)):
            raise ValueError('json knowledge must be an object or array')
    return title, document_format, body


def _hash(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def _require_key(key):
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ValueError('Invalid knowledge storage key')
    return key


def _document_path(directory, key):
    root = Path(directory).resolve()
    path = (root / _require_key(key)).resolve()
    if path.parent != root:
        raise ValueError('Invalid knowledge storage key')
    return path


def _write_temp(directory, key, body):
    path = _document_path(directory, key)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(body, encoding='utf-8', newline='\n')


def _commit_temp(directory, key):
    path = _document_path(directory, key)
    temporary = path.with_name(path.name + '.tmp')
    temporary.replace(path)


def _discard_temp(directory, key):
    path = _document_path(directory, key)
    temporary = path.with_name(path.name + '.tmp')
    if temporary.is_file():
        temporary.unlink()


def _read_body(directory, row):
    path = _document_path(directory, row['storage_key'])
    if not path.is_file():
        raise ValueError('Knowledge document is missing')
    body = path.read_text(encoding='utf-8')
    if _hash(body) != row['content_hash']:
        raise ValueError('Knowledge document does not match its record')
    return body


def _public(row, body):
    return {
        'id': row['id'],
        'datasource_id': row['datasource_id'],
        'title': row['title'],
        'format': row['format'],
        'content_hash': row['content_hash'],
        'version': row['version'],
        'provenance': 'user',
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'body': body,
        'verification': 'user_provided_unverified',
    }
