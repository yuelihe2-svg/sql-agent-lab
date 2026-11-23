"""Optional user-written knowledge for one registered data source.

Documents are user claims. They are not checked against the business database
and are not inserted into the SQL Agent prompt.
"""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import logging
import re
import threading
import uuid

from sql_agent_lab.macsql.task_lifecycle import utc_now

from .database import LOCAL_WORKSPACE_ID


FORMATS = frozenset({'markdown', 'text', 'json'})
BODY_LIMIT = 24_000
TITLE_LIMIT = 200
_KEY = re.compile(r'[0-9a-f]{32}')
_DOCUMENT_LOCK = threading.RLock()
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


@contextmanager
def _document_operation():
    # Local HTTP workers read metadata and files together. Reindex uses the same snapshot.
    with _DOCUMENT_LOCK:
        yield


@_document_operation()
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
    path = _write_body(directory, key, body)
    try:
        store.insert_knowledge(record, body=body)
    except Exception:
        _discard_body(path)
        raise
    return _public(record, body)


@_document_operation()
def update_knowledge(store, workspace_db, datasource_id, knowledge_id, title, document_format, body,
                     *, expected_version=None):
    title, document_format, body = _validate(title, document_format, body)
    current = store.get_knowledge_row(datasource_id, knowledge_id)
    directory = knowledge_dir(workspace_db)
    if expected_version is not None and (type(expected_version) is not int or expected_version != current['version']):
        raise ValueError('业务说明版本已变化，请刷新后重新编辑。')
    old_path = _document_path(directory, current['storage_key'])
    updated = {
        'id': current['id'],
        'datasource_id': datasource_id,
        'title': title,
        'format': document_format,
        'content_hash': _hash(body),
        'version': current['version'] + 1,
        'storage_key': uuid.uuid4().hex,
        'updated_at': utc_now(),
    }
    path = _write_body(directory, updated['storage_key'], body)
    try:
        store.update_knowledge(updated, body=body)
    except Exception:
        _discard_body(path)
        raise
    _discard_body(old_path)
    current.update(updated)
    return _public(current, body)


@_document_operation()
def delete_knowledge(store, workspace_db, datasource_id, knowledge_id):
    key = store.delete_knowledge(datasource_id, knowledge_id)
    path = _document_path(knowledge_dir(workspace_db), key)
    _discard_body(path)
    return {'id': knowledge_id, 'datasource_id': datasource_id, 'deleted': True}


@_document_operation()
def read_knowledge(store, workspace_db, datasource_id, knowledge_id):
    row = store.get_knowledge_row(datasource_id, knowledge_id)
    body = _read_body(knowledge_dir(workspace_db), row)
    return _public(row, body)


@_document_operation()
def list_knowledge(store, workspace_db, datasource_id):
    documents = []
    directory = knowledge_dir(workspace_db)
    for row in store.list_knowledge_rows(datasource_id):
        documents.append(_public(row, _read_body(directory, row)))
    return documents


@_document_operation()
def read_available_knowledge(store, workspace_db, datasource_id):
    """Return readable documents and ids that can no longer be verified.

    A missing or altered file is skipped. It is not replaced with empty text
    and it is not taken from another data source.
    """
    documents, skipped = [], []
    directory = knowledge_dir(workspace_db)
    for row in store.list_knowledge_rows(datasource_id):
        try:
            documents.append(_public(row, _read_body(directory, row)))
        except ValueError:
            skipped.append({'id': row['id'], 'version': row['version'], 'reason': 'unavailable'})
    return documents, skipped


@_document_operation()
def reindex_knowledge(store, workspace_db):
    """Rebuild the FTS rows from the knowledge files. Missing files are left out."""
    rows = []
    directory = knowledge_dir(workspace_db)
    for row in store.list_all_knowledge_rows():
        try:
            body = _read_body(directory, row)
        except ValueError:
            continue
        rows.append({**row, 'body': body})
    store.replace_knowledge_index(rows)
    return {'indexed': len(rows), 'status': 'rebuilt'}


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


def _write_body(directory, key, body):
    """Write a private, unique body before publishing its key in SQLite."""
    path = _document_path(directory, key)
    handle = path.open('xb')
    try:
        with handle:
            handle.write(body.encode('utf-8'))
    except Exception:
        _discard_body(path)
        raise
    return path


def _discard_body(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning('An unreferenced knowledge file could not be removed')


def _read_body(directory, row):
    path = _document_path(directory, row['storage_key'])
    if not path.is_file():
        raise ValueError('Knowledge document is missing')
    body = path.read_bytes().decode('utf-8')
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
