"""Reference-style MAC-SQL path: role activation and execution-led refinement."""

import json
import sqlite3

import pytest

pytest.importorskip('langgraph')

from sql_agent_lab.macsql.context import ContextPolicy
from sql_agent_lab.macsql.dataset import Question
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent, last_sql_block, make_agent_class
from sql_agent_lab.macsql.tools import SQLiteTools
from sql_agent_lab.macsql.reference_core import ModelReply, WorkflowConfig, ROLES, RoleOutputError


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'small.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT); '
                           "INSERT INTO people VALUES (1, 'Ada');")
    return path


class Scripted:
    def __init__(self, *, bad_sql=False, selector=None):
        self.calls = []
        self.bad_sql = bad_sql
        self.selector = selector or '{}'

    def __call__(self, *, role, model, messages, max_output_tokens):
        self.calls.append((role, messages[-1]['content']))
        if role == 'selector':
            answer = self.selector
        elif role == 'decomposer':
            first = 'SELECT id FROM people'
            last = 'SELECT absent FROM people' if self.bad_sql else 'SELECT name FROM people'
            answer = f'Sub question 1:\n```sql\n{first}\n```\nSub question 2:\n```sql\n{last}\n```'
        else:
            answer = '```sql\nSELECT name FROM people\n```'
        return ModelReply(answer, 50, 30, simulated=True)


def make_agent(database, model, *, input_budget=6000):
    config = WorkflowConfig(SQLiteTools({'db': database}), model,
                            context=ContextPolicy(input_budget=input_budget),
                            max_repairs=3)
    return ReferenceMacSQLAgent({role: 'test' for role in ROLES}, config=config)


def test_last_subsql_is_answer_and_small_schema_skips_selector(database):
    model = Scripted()
    agent = make_agent(database, model)
    assert agent.run(Question('1', 'db', 'Who is listed?')) == 'SELECT name FROM people'
    assert [role for role, _ in model.calls] == ['decomposer']
    assert 'ONE complete final SQL block' in model.calls[0][1] and 'Do not repeat intermediate SQL' in model.calls[0][1]


def test_execution_error_goes_to_refiner_without_semantic_gate(database):
    model = Scripted(bad_sql=True)
    agent = make_agent(database, model)
    assert agent.run(Question('2', 'db', 'Who is listed?')) == 'SELECT name FROM people'
    assert [role for role, _ in model.calls] == ['decomposer', 'refiner']
    assert 'no such column' in model.calls[-1][1]


def test_large_schema_invokes_selector_and_invalid_decision_falls_back(database):
    with sqlite3.connect(database) as conn:
        for number in range(12):
            conn.execute(f'CREATE TABLE unrelated_{number} (' + ','.join(
                f'field_{i} TEXT' for i in range(15)) + ')')
    model = Scripted(selector=json.dumps({'people': 'keep_all'}))
    agent = make_agent(database, model, input_budget=3800)
    assert agent.run(Question('3', 'db', 'Who is listed?')) == 'SELECT name FROM people'
    assert [role for role, _ in model.calls] == ['selector', 'decomposer']
    assert 'unrelated_0' not in model.calls[-1][1]


def test_missing_sql_block_is_explicit_failure():
    with pytest.raises(RoleOutputError, match='fenced SQL'):
        last_sql_block('Here is a plan without SQL')


def test_agentopt_constructor_contract(database):
    config = WorkflowConfig(SQLiteTools({'db': database}), Scripted())
    agent_class = make_agent_class(config)
    assert agent_class({role: 'test' for role in ROLES}).run(Question('4', 'db', 'Who is listed?')) == 'SELECT name FROM people'
