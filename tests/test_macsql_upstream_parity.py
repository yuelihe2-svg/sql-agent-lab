"""Optional offline comparisons with reviewed pure functions from pinned source.

No upstream module is imported. Only the listed AST function bodies are compiled;
provider imports, constructors, SQL I/O and ChatManager startup never execute.
CI without the ignored local source skips these comparisons, never downloads it.
"""
import ast
from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

from sql_agent_lab.experiments.macsql_reproduction import parse_final_sql, parse_selection, refinement_reason
from sql_agent_lab.experiments.macsql_schema import BirdSchema, value_examples
from sql_agent_lab.experiments.macsql_upstream import REVISION, load_prompts, verified_sources

ROOT = Path(__file__).resolve().parents[1] / '.local' / 'macsql-upstream' / REVISION


@pytest.fixture(scope='module')
def upstream():
    if not ROOT.exists():
        pytest.skip('Pinned local upstream source not installed; no automatic network access')
    sources = verified_sources(ROOT)
    functions = {'is_valid_date', 'is_valid_date_column', 'is_email', 'check_selector_response',
                 'parse_json', 'parse_sql_from_string'}
    methods = {'Selector': {'_get_db_desc_str', '_build_bird_table_schema_list_str',
                            '_get_value_examples_str', '_is_need_prune'},
               'Refiner': {'_is_need_refine'}}
    nodes = [n for n in ast.parse(sources['core/utils.py']).body
             if isinstance(n, ast.FunctionDef) and n.name in functions]
    for node in ast.parse(sources['core/agents.py']).body:
        if isinstance(node, ast.ClassDef) and node.name in methods:
            node.bases, node.keywords, node.decorator_list = [], [], []
            node.body = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in methods[node.name]]
            nodes.append(node)
    namespace = dict(re=re, json=json, Dict=dict, List=list, deepcopy=deepcopy)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<reviewed-upstream-pure-functions>', 'exec'), namespace)
    return namespace


def test_exact_prompts_load_without_execution(upstream, tmp_path):
    prompts = load_prompts(ROOT)
    assert set(prompts) == {'selector_template', 'decompose_template_bird', 'refiner_template'}
    for name, prompt in prompts.items():
        # Exercise original brace escaping, including few-shot JSON/SQL examples.
        formatted = prompt.format(db_id='synthetic', query='QUESTION_SENTINEL', evidence='EVIDENCE_SENTINEL',
                                  desc_str='SCHEMA_SENTINEL', fk_str='FK_SENTINEL', sql='SELECT 1',
                                  sqlite_error='synthetic', exception_class='synthetic')
        assert 'QUESTION_SENTINEL' in formatted and 'SCHEMA_SENTINEL' in formatted
    # Source changes cannot silently become a new experimental condition.
    for name, text in verified_sources(ROOT).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode('utf8'))
    (tmp_path / 'core/const.py').write_text('raise RuntimeError("must never execute")', encoding='utf8')
    with pytest.raises(ValueError, match='checksum mismatch'):
        load_prompts(tmp_path)


def test_schema_render_and_trigger_equal_official(upstream):
    tables = {
        'a': dict(columns=[dict(name=f'a{i}', full_name=f'column {i}', examples="['sample']")
                           for i in range(9)], primary_keys=['a8'], foreign_keys=[('a7', 'b', 'b0')]),
        'b': dict(columns=[dict(name=f'b{i}', full_name=f'column {i}', examples='')
                           for i in range(4)], primary_keys=['b0'], foreign_keys=[]),
    }
    schema = BirdSchema('synthetic', tables)
    selector = upstream['Selector']()
    selector.db2infos = {'synthetic': dict(
        desc_dict={t: [(c['name'], c['full_name'], '') for c in d['columns']] for t, d in tables.items()},
        value_dict={t: [(c['name'], c['examples']) for c in d['columns']] for t, d in tables.items()},
        pk_dict={t: d['primary_keys'] for t, d in tables.items()},
        fk_dict={t: d['foreign_keys'] for t, d in tables.items()},
    )}
    selector.db2dbjsons = {'synthetic': dict(avg_column_count=13 // 2, total_column_count=13)}
    assert selector._is_need_prune('synthetic', '') == schema.needs_selector
    for decisions in ({}, {'a': 'drop_all'}, {'a': ['a5']}, {'a': [], 'b': 'drop_all'}):
        assert schema.render(decisions) == tuple(selector._get_db_desc_str('synthetic', decisions))


def test_parsers_equal_official(upstream):
    for reply in ('bad', '```sql SELECT 1 ``` then ```sql SELECT 2 ```', '```SQL SELECT 1 ```'):
        assert parse_final_sql(reply) == upstream['parse_sql_from_string'](reply)
    for reply in ('bad', '```json {"a":["a0"], "b":"drop_all"} ```', '```json [] ```', '{"a": "keep_all"}'):
        assert parse_selection(reply) == upstream['parse_json'](reply)


def test_values_and_refinement_equal_official(upstream):
    selector = upstream['Selector']()
    selector.dataset_name = 'bird'
    cases = [([], 'TEXT'), ([None, 'alpha', '', 'beta'], 'TEXT'),
             (list(range(11)), 'INTEGER'), (['2020-01-01 00:01:02', '2021-05-06'], 'TEXT'),
             (['a@example.org'], 'TEXT'), (['x' * 51], 'TEXT'), ([1, 2, None], 'INTEGER')]
    for values, kind in cases:
        assert value_examples(values, kind) == selector._get_value_examples_str(values, kind)
    refiner = upstream['Refiner']()
    refiner.dataset_name = 'bird'
    for feedback in ({'data': []}, {'data': [[None]]}, {'data': [[0]]}, {'sqlite_error': 'bad SQL'}):
        original = deepcopy(feedback)
        assert bool(refinement_reason(feedback)) == refiner._is_need_refine(original)
