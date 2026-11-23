"""Application metadata lives in its own SQLite file and survives a restart."""
from contextlib import closing
from dataclasses import replace
import inspect
from pathlib import Path
import sqlite3
import uuid

import pytest

from sql_agent_lab.macsql import live_ui
from sql_agent_lab.workspace.database import (
    APP_TABLES, FORBIDDEN_COLUMNS, SCHEMA_VERSION, WorkspaceStore, column_names, migrate,
    remove_database,
)
from sql_agent_lab.workspace.models import Conversation, DataSource, Message, User, Workspace


NOW = '2026-11-18T20:00:00+00:00'
LATER = '2026-11-18T20:05:00+00:00'
WS = str(uuid.UUID(int=1))
USER = str(uuid.UUID(int=2))
CONV = str(uuid.UUID(int=3))
MSG = str(uuid.UUID(int=4))


def tables(path):
    with closing(sqlite3.connect(path)) as conn:
        return sorted(row[0] for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"))


def versions(path):
    with closing(sqlite3.connect(path)) as conn:
        return [row[0] for row in conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version')]


def business_database(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE secrets(password TEXT, sql TEXT)')
        conn.execute(
            "INSERT INTO secrets VALUES ('credential-sentinel-value', "
            "'SELECT gold_sentinel FROM answer')")
        conn.commit()
    return path.read_bytes()


def test_migration_is_idempotent_and_isolated(tmp_path):
    database = tmp_path / 'nested' / 'workspace.sqlite'
    migrate(database)
    first_tables = tables(database)
    assert database.is_relative_to(tmp_path)
    assert set(first_tables) == APP_TABLES
    assert versions(database) == list(range(1, SCHEMA_VERSION + 1))
    assert column_names(database).isdisjoint(FORBIDDEN_COLUMNS)

    migrate(database)
    assert tables(database) == first_tables
    assert versions(database) == list(range(1, SCHEMA_VERSION + 1))
    WorkspaceStore(database)

    business = tmp_path / 'SECRET_DB_PATH_marker.sqlite'
    before = business_database(business)
    assert business.read_bytes() == before
    with pytest.raises(ValueError, match='not an application workspace'):
        migrate(business)
    assert business.read_bytes() == before
    with pytest.raises(ValueError, match='not an application workspace'):
        remove_database(business)
    assert business.read_bytes() == before

    empty = tmp_path / 'empty.sqlite'
    with closing(sqlite3.connect(empty)):
        pass
    empty_before = empty.read_bytes()
    with pytest.raises(ValueError, match='not an application workspace'):
        migrate(empty)
    assert empty.read_bytes() == empty_before

    junk = tmp_path / 'notes.bin'
    junk.write_bytes(b'credential-sentinel-value')
    with pytest.raises(ValueError, match='not an application workspace'):
        migrate(junk)
    assert junk.read_bytes() == b'credential-sentinel-value'

    remove_database(database)
    assert not database.exists()
    migrate(database)
    assert versions(database) == list(range(1, SCHEMA_VERSION + 1))


def test_conversations_survive_restart_and_bind_one_data_source(tmp_path):
    business = tmp_path / 'SECRET_DB_PATH_marker.sqlite'
    before = business_database(business)
    store = WorkspaceStore(tmp_path / 'workspace.sqlite')
    store.add_workspace(Workspace(WS, 'Local', NOW))
    store.add_user(User(USER, 'Ada', NOW))
    store.add_data_source(DataSource(
        'shop_a', WS, 'Shop A', 'sqlite', NOW, secret_ref='sources:shop_a'))
    conversation = Conversation(CONV, WS, USER, 'shop_a', 'How many orders?', NOW, NOW)
    store.add_conversation(conversation)
    message = Message(MSG, 1, 'user', 'How many orders?', LATER, CONV)
    store.add_message(message)
    with pytest.raises(ValueError, match='not registered'):
        store.add_conversation(replace(conversation, id=str(uuid.UUID(int=9)), datasource_id='other_shop'))
    other = Workspace(str(uuid.UUID(int=8)), 'Other', NOW)
    store.add_workspace(other)
    with pytest.raises(ValueError, match='another workspace'):
        store.add_conversation(replace(
            conversation, id=str(uuid.UUID(int=10)), workspace_id=other.id))
    with pytest.raises(ValueError, match='seq must be 2'):
        store.add_message(Message(str(uuid.UUID(int=11)), 3, 'assistant', 'Completed.', LATER, CONV))

    reopened = WorkspaceStore(store.path)
    loaded = reopened.get_conversation(CONV)
    assert loaded.datasource_id == 'shop_a'
    assert loaded.updated_at == LATER
    assert reopened.list_messages(CONV) == [message]
    assert reopened.get_data_source('shop_a').catalog() == {
        'id': 'shop_a', 'name': 'Shop A', 'kind': 'sqlite', 'read_only': True}
    blob = store.path.read_bytes()
    assert b'SECRET_DB_PATH_marker' not in blob
    assert b'credential-sentinel-value' not in blob
    assert b'gold_sentinel' not in blob
    assert b'shop_a.sqlite' not in blob
    assert business.read_bytes() == before
    text = loaded.to_json() + message.to_json()
    assert 'shop_a.sqlite' not in text
    assert 'credential-sentinel-value' not in text


def test_workspace_database_is_opt_in():
    assert inspect.signature(live_ui.Observatory.__init__).parameters['workspace_db'].default is None
    parser = live_ui.argument_parser()
    assert parser.parse_args(['--output-dir', 'runs']).workspace_db is None
    assert parser.parse_args(['--output-dir', 'runs', '--workspace-db', 'test.db']).workspace_db == Path('test.db')
