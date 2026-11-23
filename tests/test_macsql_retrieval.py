"""Bounded metadata retrieval and value grounding in the reference workflow."""

import sqlite3

import pytest

pytest.importorskip('langgraph')

from sql_agent_lab.macsql.context import ContextPolicy
from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent, retrieval_hints
from sql_agent_lab.macsql.reference_core import ModelReply, ROLES, WorkflowConfig
from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'school.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE schools (id INTEGER PRIMARY KEY, name TEXT); '
                           'CREATE TABLE scores (school_id INTEGER, avg_math REAL, '
                           'FOREIGN KEY (school_id) REFERENCES schools(id)); '
                           "INSERT INTO schools VALUES (1, 'A% Academy'), (2, 'ABC School'); "
                           'INSERT INTO scores VALUES (1, 91.0), (2, 83.0);')
    return path


def make_tools(database):
    docs = {'school': {'schools': {'name': {'description': 'School name', 'source': 'test-doc'}},
                       'scores': {'avg_math': {'description': 'Average mathematics score',
                                               'source': 'test-doc'}}}}
    return SQLiteTools({'school': database}, ToolLimits(timeout_seconds=2), metadata=docs)


def test_profile_and_agent_share_generated_columns_and_handle_binary_samples(database):
    from sql_agent_lab.workspace.profile import extract_profile
    with sqlite3.connect(database) as conn:
        conn.execute('CREATE TABLE sqliteOrders (qty INT, price INT, '
                     'total INT GENERATED ALWAYS AS (qty * price), receipt BLOB)')
        conn.execute('INSERT INTO sqliteOrders(qty, price, receipt) VALUES (2, 3, ?)', (bytes([255]),))
        conn.execute('CREATE VIRTUAL TABLE articles USING fts5(title, body)')
    tools = make_tools(database)
    schema = tools.load_schema('school', enrich=True)
    assert set(schema['articles']['columns']) == {'title', 'body'}
    assert not any(name.startswith('articles_') for name in schema)
    profile = next(t for t in extract_profile(database)['tables'] if t['name'] == 'sqliteOrders')
    assert list(schema['sqliteOrders']['columns']) == [c['name'] for c in profile['columns']]
    assert schema['sqliteOrders']['columns']['receipt']['observed_samples'] == ['[BLOB]']
    assert tools.execute_sql_readonly('school', 'SELECT total FROM sqliteOrders')['preview_rows'] == [[6]]


def test_fts_finds_documented_column_and_value_lookup_escapes_wildcards(database):
    tools = make_tools(database)
    hits = tools.search_schema('school', 'Which school has the highest average mathematics score?')
    assert any(hit['table'] == 'scores' and hit['column'] == 'avg_math' and
               hit['source'] == 'test-doc' for hit in hits)
    before = database.read_bytes()
    match = tools.lookup_values('school', 'schools', 'name', 'A%')
    assert match['status'] == 'ok' and match['values'] == ['A% Academy']
    assert match['sample_only'] and database.read_bytes() == before
    with pytest.raises(ValueError, match='exact existing'):
        tools.lookup_values('school', 'schools; DROP TABLE schools', 'name', 'A%')


def test_hints_quote_sql_identifiers_with_spaces_and_punctuation():
    hint = retrieval_hints([{'table': 'frpm', 'column': 'FRPM Count (K-12)',
                             'description': 'Meal count'}], [])
    assert '"frpm"."FRPM Count (K-12)"' in hint
    assert 'double quotes' in hint


def test_short_binary_and_infinite_real_have_json_safe_previews(database):
    import json
    tools = make_tools(database)
    view = tools.execute_sql_readonly('school', "SELECT x'ff', 1e999")
    assert view['preview_rows'] == [['0xff', 'inf']]
    assert view['truncated'] is False
    assert view['output_type_profile']['columns'][1]['storage_classes'] == {'real': 1}
    json.dumps(view, allow_nan=False)


def test_retrieval_hints_enter_decomposer_without_changing_role_order(database):
    class Model:
        def __init__(self):
            self.calls = []

        def __call__(self, *, role, model, messages, max_output_tokens):
            self.calls.append((role, messages[-1]['content']))
            return ModelReply('```sql\nSELECT name FROM schools LIMIT 1\n```', 40, 20,
                              simulated=True)

    model = Model()
    events = []
    config = WorkflowConfig(make_tools(database), model,
                            context=ContextPolicy(input_budget=6000), trace=events.append,
                            schema_retrieval=True, value_lookup=True)
    agent = ReferenceMacSQLAgent({role: 'test' for role in ROLES}, config=config)
    sql = agent.run(Question('1', 'school', 'Which school named "A%" has the highest average mathematics score?'))
    assert sql == 'SELECT name FROM schools LIMIT 1'
    assert [role for role, _ in model.calls] == ['decomposer']
    assert 'Schema search hints' in model.calls[0][1]
    retrieval = next(event for event in events if event['event'] == 'schema_retrieved')
    assert len(retrieval['value_lookups']) <= 2
    assert any(item.get('values') == ['A% Academy'] for item in retrieval['value_lookups'])
