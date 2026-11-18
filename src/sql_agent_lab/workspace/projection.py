"""Read-only AgentRun and user Message views of an existing run directory.

The directory stays the source of truth. This module does not write the
application database or the run files, and it does not open evaluation evidence.
"""
from pathlib import Path
import json
import uuid

from sql_agent_lab.macsql.task_lifecycle import task_view

from .models import AgentRun, Message


LEGACY_MESSAGE_NAMESPACE = uuid.UUID('6d0c8a1e-4b2f-5d77-9a10-0c3e5f7b8d21')


def project_legacy_run(directory, *, active=False, managed=False, stop_requested=False):
    """Project manifest.json, task.json, and result.json.

    Returns `(run, user_message)`. `user_message` is None when the manifest has
    no question text. `conversation_id` stays empty: a custom question id is not
    a conversation.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError('Run directory does not exist')
    manifest_path = directory / 'manifest.json'
    if not manifest_path.is_file():
        raise ValueError('manifest.json is required')
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError('manifest.json must contain an object')
    question = manifest.get('question')
    if not isinstance(question, dict):
        raise ValueError('manifest.question must be an object')
    result_path = directory / 'result.json'
    result = _read_json(result_path) if result_path.is_file() else None
    if result is not None and not isinstance(result, dict):
        raise ValueError('result.json must contain an object')
    view = task_view(directory, result, active=active, managed=managed,
                     stop_requested=stop_requested)
    datasource_id = question.get('db_id')
    if not isinstance(datasource_id, str):
        raise ValueError('manifest.question.db_id is required')
    text = question.get('question', '')
    if not isinstance(text, str):
        raise ValueError('manifest.question.question must be a string')
    run_id = directory.name
    message = None
    if text.strip():
        created_at = manifest.get('created_at') or view.get('started_at') or view.get('updated_at')
        if not created_at:
            raise ValueError('A question projection needs a timestamp')
        message = Message(
            id=str(uuid.uuid5(LEGACY_MESSAGE_NAMESPACE, run_id)),
            conversation_id=None,
            seq=1,
            role='user',
            body=text.strip(),
            created_at=created_at)
    score = None
    result_status = None
    if result is not None:
        if 'score' in result:
            score = result.get('score')
        status = result.get('status')
        result_status = status or None
    review = manifest.get('semantic_review', False)
    return AgentRun(
        id=run_id,
        conversation_id=None,
        user_message_id=None if message is None else message.id,
        datasource_id=datasource_id,
        state=view.get('state'),
        stage=view.get('stage') or None,
        started_at=view.get('started_at'),
        updated_at=view.get('updated_at'),
        ended_at=view.get('ended_at'),
        stop_requested_at=view.get('stop_requested_at'),
        error=view.get('error') or None,
        result_status=result_status,
        score=score,
        semantic_review=review if isinstance(review, bool) else False,
        artifact_dir=str(directory.resolve()),
    ), message


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf8'))
    except json.JSONDecodeError as error:
        raise ValueError(f'{path.name} is not valid JSON') from error
