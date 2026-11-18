"""Public metadata JSON has no server path, credential, or reference SQL."""
from dataclasses import fields
import json
from pathlib import Path
import uuid

import pytest

from sql_agent_lab.macsql.data_sources import SQLiteSource
from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.workspace.models import (
    AgentRun, AuditEvent, Conversation, DataSource, DataSourceGrant, Membership,
    Message, User, Workspace, data_source_from_catalog, public_round_trip,
)


NOW = '2026-11-18T20:00:00+00:00'
WS = str(uuid.UUID(int=1))
USER = str(uuid.UUID(int=2))


def test_question_cannot_carry_gold_sql():
    assert [item.name for item in fields(Question)] == [
        'question_id', 'db_id', 'question', 'evidence']
    with pytest.raises(TypeError):
        Question('1', 'demo', 'Return n', SQL='SELECT gold_sentinel FROM answer')


def test_public_json_round_trip_omits_paths_and_secrets():
    workspace = Workspace(WS, 'Local', NOW)
    user = User(USER, 'Ada', NOW)
    membership = Membership(WS, USER, 'analyst')
    source = DataSource(
        'shop_a', WS, 'Shop A', 'sqlite', NOW, secret_ref='sources:shop_a',
        schema_fingerprint='ab' * 16)
    grant = DataSourceGrant(USER, 'shop_a', True)
    conversation = Conversation(
        str(uuid.UUID(int=3)), WS, USER, 'shop_a', 'How many orders?', NOW, NOW)
    message = Message(
        str(uuid.UUID(int=4)), 1, 'user', 'How many orders?', NOW, conversation.id)
    audit = AuditEvent(
        str(uuid.UUID(int=5)), NOW, 'source.register', 'data_source', 'shop_a', 'ok',
        {'status': 'ready'}, actor_user_id=USER, workspace_id=WS)
    for model in (workspace, user, membership, source, grant, conversation, message, audit):
        assert public_round_trip(model) == model
        payload = json.loads(model.to_json())
        assert set(payload).isdisjoint({
            'path', 'password', 'sql', 'gold_sql', 'reference_sql', 'artifact_dir'})

    leaked = json.loads(source.to_json())
    leaked['path'] = r'C:\secret\shop_a.sqlite'
    leaked['password'] = 'credential-sentinel'
    leaked['SQL'] = 'SELECT gold_sentinel FROM answer'
    with pytest.raises(ValueError):
        DataSource.from_json(json.dumps(leaked))


def test_agent_run_json_drops_the_server_directory():
    run = AgentRun(
        '20261118-120000-abcdef', 'shop_a', 'completed', semantic_review=False,
        result_status='completed', score=1.0,
        artifact_dir=r'C:\secret-runs\20261118-120000-abcdef')
    text = run.to_json()
    assert 'secret-runs' not in text
    assert 'artifact_dir' not in text
    restored = AgentRun.from_json(text)
    assert restored.artifact_dir is None
    assert restored.score == 1
    assert restored.to_json() == text
    with pytest.raises(ValueError):
        AgentRun.from_json(json.dumps({**json.loads(text), 'artifact_dir': r'C:\secret-runs'}))


def test_catalog_binding_accepts_only_public_source_fields():
    listed = SQLiteSource('shop_a', 'Shop A', Path(r'C:\secret\shop_a.sqlite')).public()
    source = data_source_from_catalog(WS, listed, secret_ref='sources:shop_a', created_at=NOW)
    assert source.id == 'shop_a'
    assert source.catalog() == listed
    text = source.to_json()
    assert 'shop_a.sqlite' not in text
    assert r'C:\secret' not in text
    with pytest.raises(ValueError):
        data_source_from_catalog(WS, {**listed, 'path': 'shop_a.sqlite'}, secret_ref='sources:shop_a',
                                 created_at=NOW)
    with pytest.raises(TypeError):
        DataSource('shop_a', WS, 'Shop A', 'sqlite', NOW, password='credential-sentinel')


def test_first_version_flags_and_audit_detail_stay_narrow():
    with pytest.raises(ValueError):
        Workspace(WS, 'Local', NOW, local_only=False)
    with pytest.raises(ValueError):
        DataSource('bad id', WS, 'Shop', 'sqlite', NOW)
    with pytest.raises(ValueError):
        DataSource('shop_a', WS, 'Shop', 'sqlite', NOW, read_only=False)
    with pytest.raises(ValueError):
        AgentRun('run-1', 'shop_a', 'completed', score=2)
    with pytest.raises(ValueError):
        AuditEvent(str(uuid.UUID(int=5)), NOW, 'sql.execute', 'statement', 'run-1', 'ok',
                   {'sql': 'SELECT gold_sentinel FROM answer'})
    with pytest.raises(ValueError):
        AuditEvent(str(uuid.UUID(int=5)), NOW, 'sql.execute', 'statement', 'run-1', 'denied',
                   {'rows': [['business-row']]})
