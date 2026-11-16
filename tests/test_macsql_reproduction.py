"""Synthetic SQLite and scripted replies only; no model or shared DB access."""
import json
import sqlite3

import httpx
import pytest

from sql_agent_lab.experiments import macsql_reproduction as reproduction
from sql_agent_lab.experiments.macsql_schema import BirdSchema, prepare_schema, value_examples
from sql_agent_lab.macsql.dataset import Question, Reference
from sql_agent_lab.macsql.deepseek_api import Ledger, MODEL
from sql_agent_lab.macsql.reference_core import ModelReply
from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits


def columns(count):
    return [dict(name=f'c{i}', full_name=f'column {i}', examples='') for i in range(count)]


def schema_with_counts(counts):
    return BirdSchema('db', {f't{i}': dict(columns=columns(n), primary_keys=[], foreign_keys=[])
                             for i, n in enumerate(counts)})


@pytest.mark.parametrize('counts,expected', [([6], False), ([7], True), ([6]*5, False),
                                            ([6]*6, True), ([6, 7], False), ([7, 7], True)])
def test_selector_uses_floor_average_and_total(counts, expected):
    assert schema_with_counts(counts).needs_selector == expected


def test_selection_preserves_upstream_surprising_rules():
    schema = schema_with_counts([9, 4, 8])
    schema.tables['t0']['primary_keys'] = ['c8']
    schema.tables['t0']['foreign_keys'] = [('c7', 't1', 'c1')]
    text, fk, chosen = schema.render({'t0': ['c5'], 't1': 'drop_all', 'unknown': 'keep_all'})
    assert chosen['t0'] == ['c5', 'c7', 'c8', 'c0', 'c1', 'c2']
    assert chosen['t1'] == ['c0', 'c1', 'c2', 'c3']  # drop_all still keeps first six
    assert len(chosen['t2']) == 8  # omitted table is kept, not removed
    assert fk == 't0.`c7` = t1.`c1`' and '# Table: t1' in text


@pytest.mark.parametrize('text,expected', [
    ('reason\n```json\n{"t":"drop_all"}\n```', {'t': 'drop_all'}),
    ('{"t":"keep_all"}', {}), ('```json\n{"t":"bad"}\n```', {}),
    ('```json\n[]\n```', {}), ('```json\n{"t":["c1"]}\n```', {'t': ['c1']}),
])
def test_original_json_fence_contract(text, expected):
    assert reproduction.parse_selection(text) == expected


@pytest.mark.parametrize('reply,expected', [
    ('First\n```sql\nSELECT 1\n```\nFinal\n```sql\nSELECT 2\n```', 'SELECT 2'),
    ('SELECT 1', 'error: No SQL found in the input string'),
    ('```SQL\nSELECT 1\n```', 'error: No SQL found in the input string'),
])
def test_only_last_lowercase_sql_fence_is_used(reply, expected):
    assert reproduction.parse_final_sql(reply) == expected


@pytest.fixture
def tools_and_schema(tmp_path):
    path = tmp_path / 'test.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE t(n INTEGER, label TEXT);'
                          "INSERT INTO t VALUES(42,'alpha'),(7,'beta'),(8,'alpha');")
    tools = SQLiteTools({'db': path})
    entry = dict(db_id='db', table_names_original=['t'], table_names=['t'],
                 column_names_original=[[-1, '*'], [0, 'n'], [0, 'label']],
                 column_names=[[-1, '*'], [0, 'number_value'], [0, 'label']],
                 primary_keys=[], foreign_keys=[])
    return tools, prepare_schema(tools, entry), entry


@pytest.fixture
def scripted(monkeypatch, tools_and_schema):
    # Synthetic templates isolate control-flow tests; real templates are checked
    # independently against the pinned source when that local source is present.
    monkeypatch.setattr(reproduction, 'load_prompts', lambda _: {
        'selector_template': 'Select {db_id} {query} {evidence} {desc_str} {fk_str}',
        'decompose_template_bird': 'Decompose {query} {evidence} {desc_str} {fk_str}',
        'refiner_template': 'Repair {query} {evidence} {desc_str} {fk_str} {sql} {sqlite_error} {exception_class}',
    })

    def run(sqls, *, raw=False, schema=None, tools=None, **options):
        calls, events = [], []
        responses = iter(sqls)

        def model(**kwargs):
            calls.append(kwargs)
            reply = next(responses)
            return ModelReply(reply if raw else '```sql\n' + reply + '\n```', 1, 1, simulated=True)

        agent = reproduction.OriginalMacSQLAgent(
            tools=tools or tools_and_schema[0], schema=schema or tools_and_schema[1],
            model=model, model_name='fake', upstream_dir='unused', trace=events.append, **options)
        result = agent.run(Question('synthetic', 'db', 'Return n', 'Use t'))
        return result, calls, events
    return run


def test_single_pass_decomposition_and_single_user_message(scripted):
    result, calls, _ = scripted(['subquery ```sql SELECT 999 ``` final ```sql SELECT n FROM t ```'], raw=True)
    assert result.sql == 'SELECT n FROM t' and len(calls) == 1
    assert [m['role'] for m in calls[0]['messages']] == ['user']
    assert result.final_sql_checked and result.stop_reason == 'execution_accepted'


def test_large_schema_calls_selector_then_decomposer(scripted):
    result, calls, events = scripted(['```json {"t0":"drop_all"} ```', '```sql SELECT 42 ```'],
                                     raw=True, schema=schema_with_counts([9]))
    assert result.selector_invoked
    assert [c['role'] for c in calls] == ['selector', 'decomposer']
    chosen = next(e for e in events if e['event'] == 'schema_selected')['selected_columns']
    assert len(chosen['t0']) == 6


def test_invalid_selector_reply_falls_back_to_full_schema(scripted):
    _, calls, events = scripted(['not JSON', '```sql SELECT 42 ```'],
                                raw=True, schema=schema_with_counts([9]))
    assert len(calls) == 2
    chosen = next(e for e in events if e['event'] == 'schema_selected')['selected_columns']
    assert len(chosen['t0']) == 9


@pytest.mark.parametrize('bad', ['SELECT missing FROM t', 'SELECT n FROM t WHERE 0', 'SELECT NULL'])
def test_error_empty_and_null_repair(scripted, bad):
    result, calls, _ = scripted([bad, 'SELECT n FROM t'])
    assert result.sql == 'SELECT n FROM t' and result.repairs == 1 and result.rounds == 2
    assert [c['role'] for c in calls] == ['decomposer', 'refiner']


def test_third_repair_is_returned_unexecuted_and_not_rolled_back(scripted):
    result, calls, events = scripted(['SELECT missing FROM t'] * 3 + ['SELECT n FROM t'])
    assert result.repairs == 3 and result.rounds == 3 and len(calls) == 4
    assert result.sql == 'SELECT n FROM t' and not result.final_sql_checked
    assert result.checked_sql == 'SELECT missing FROM t' and result.stop_reason == 'round_limit'
    assert len([e for e in events if e['event'] == 'execution']) == 3


def test_repair_does_not_apply_output_type_or_semantic_gates(scripted):
    result, calls, _ = scripted(["SELECT 'not the requested answer'"])
    assert len(calls) == 1 and result.stop_reason == 'execution_accepted'


@pytest.mark.parametrize('sql', ['No SQL provided', "```sql SELECT 'error' ```"])
def test_original_error_guard_does_not_invoke_refiner(scripted, sql):
    result, calls, _ = scripted([sql], raw=True)
    assert result.stop_reason == 'upstream_error_guard' and len(calls) == 1
    assert not result.final_sql_checked


def test_null_in_thirteenth_column_is_not_hidden_by_ui_preview(scripted):
    result, calls, _ = scripted(['SELECT ' + ','.join(['1'] * 12 + ['NULL']), 'SELECT 42'])
    assert result.repairs == 1 and len(calls) == 2


def test_null_after_first_five_rows_does_not_trigger_refiner(scripted):
    result, calls, _ = scripted(['SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 '
                                'UNION ALL SELECT 4 UNION ALL SELECT 5 UNION ALL SELECT NULL'])
    assert result.repairs == 0 and len(calls) == 1


def test_read_only_rejects_write_and_database_remains_unchanged(scripted, tools_and_schema):
    result, _, _ = scripted(['DELETE FROM t', 'SELECT n FROM t'])
    assert result.repairs == 1
    with tools_and_schema[0].connection('db') as conn:
        assert conn.execute('SELECT count(*) FROM t').fetchone()[0] == 3


def test_resource_limit_stops_without_misreporting_empty_or_success(scripted, tools_and_schema):
    tools = SQLiteTools(tools_and_schema[0].databases, ToolLimits(max_result_rows=1))
    result, calls, _ = scripted(['SELECT n FROM t'], tools=tools)
    assert result.stop_reason == 'resource_limit' and len(calls) == 1
    assert 'data' not in result.last_execution


def test_timeout_stops_without_model_repair(scripted, tools_and_schema, monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def interrupted(*args, **kwargs):
        error = sqlite3.OperationalError('interrupted')
        error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
        raise error
        yield  # Mark as a context manager; execution always raises above.

    monkeypatch.setattr(tools_and_schema[0], 'connection', interrupted)
    result, calls, _ = scripted(['SELECT n FROM t'])
    assert result.stop_reason == 'execution_timeout' and len(calls) == 1


def test_with_prefix_quirk_is_preserved_in_refiner_prompt(scripted):
    _, calls, _ = scripted(['WITH x AS (SELECT NULL AS n) SELECT n FROM x', 'SELECT 42'])
    assert 'SELECTWITH x' in calls[1]['messages'][0]['content']


def test_prompt_limit_fails_before_call_instead_of_compacting(scripted):
    with pytest.raises(ValueError, match='no request sent'):
        scripted(['SELECT 1'], max_prompt_bytes=2)


def test_preparation_uses_frequency_and_metadata_names(tools_and_schema):
    _, schema, _ = tools_and_schema
    cols = schema.tables['t']['columns']
    assert cols[0]['full_name'] == 'number value'
    assert cols[1]['examples'] == "['alpha', 'beta']"


def test_metadata_mismatch_is_not_silently_substituted(tools_and_schema):
    tools, _, entry = tools_and_schema
    with pytest.raises(ValueError, match='tables differ'):
        prepare_schema(tools, {**entry, 'table_names_original': ['wrong']})


@pytest.mark.parametrize('values,kind,expected', [
    (list(range(11)), 'INTEGER', ''), ([None, '', 'alpha'], 'TEXT', "[None, 'alpha']"),
    (['2020-01-01', '2021-12-31'], 'TEXT', "['2020-01-01']"),
    (['https://example.org'], 'TEXT', ''), (['a@example.org'], 'TEXT', ''),
])
def test_original_value_filters(values, kind, expected):
    assert value_examples(values, kind) == expected


def test_reference_objects_rejected_before_graph(scripted, tools_and_schema, monkeypatch):
    # scripted fixture has installed the synthetic templates.
    agent = reproduction.OriginalMacSQLAgent(
        tools=tools_and_schema[0], schema=tools_and_schema[1], model=None,
        model_name='fake', upstream_dir='unused')
    with pytest.raises(TypeError):
        agent.run(Reference('db', 'SELECT 42 AS GOLD_SENTINEL'))


def test_deepseek_profile_uses_text_temperature_and_existing_ledger(tmp_path):
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=dict(id='synthetic', model=MODEL,
            usage=dict(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            choices=[dict(finish_reason='stop', message=dict(content='```json\n{}\n```'))]))

    ledger = Ledger(tmp_path / 'calls.jsonl', max_calls=5)
    model = reproduction.ReproductionDeepSeekModel('dummy-not-a-secret', ledger, 'fake',
                                                   transport=httpx.MockTransport(respond))
    reply = model(role='selector', model=MODEL, messages=[dict(role='user', content='synthetic')],
                  max_output_tokens=3200)
    assert payloads[0]['temperature'] == 0.1 and 'response_format' not in payloads[0]
    assert reply.raw_usage['response_format_contract'] == 'text'
    assert ledger.totals() == (15, 1, False)
