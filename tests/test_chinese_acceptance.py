import json
import sqlite3
from pathlib import Path
import pytest
from sql_agent_lab.workspace.answer_checks import clarification, query_basis
from sql_agent_lab.workspace.chat_intent import direct_answer

CASES = json.loads((Path(__file__).parent / 'fixtures/chinese_acceptance.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('case', CASES, ids=lambda c: str(c['id']))
def test_acceptance_oracles(case):
    if 'expected_sql' in case:
        offset = 1000 if case['source'] == 'archive' else 0
        with sqlite3.connect(':memory:') as conn:
            conn.execute('CREATE TABLE orders(id INTEGER, city TEXT, year INTEGER, status TEXT, amount INTEGER)')
            rows = [(1,'上海',2024,'paid',100),(2,'上海',2024,'paid',200),(3,'上海',2024,'cancelled',900),(4,'杭州',2024,'paid',400),(5,'杭州',2023,'paid',800),(6,'上海',2023,'paid',700)]
            conn.executemany('INSERT INTO orders VALUES(?,?,?,?,?)', [(*r[:4], r[4]+offset) for r in rows])
            assert [list(r) for r in conn.execute(case['expected_sql'])] == case['expected_rows']
    elif case['kind'] == 'clarify':
        assert case['expected'] in clarification(case['question'], [])
    elif case['kind'] in ('metadata', 'knowledge'):
        assert case['expected'] in direct_answer(case['question'], lambda: {'tables':[{'name':'orders','columns':[]}]})
    else:
        assert direct_answer(case['question'], lambda: {}) is None


def test_basis_preserves_or_and_complexity():
    text = query_basis("SELECT SUM(amount) FROM orders WHERE year=2024 AND (city='上海' OR city='杭州')")
    assert 'OR' in text and '2024' in text and 'SUM' in text
    assert '复杂' in query_basis('SELECT COUNT(*) FROM orders a JOIN orders b ON a.id=b.id')
    assert '全部记录' in query_basis('SELECT COUNT(*) FROM orders')


def test_year_change_uses_latest_city():
    from sql_agent_lab.workspace.answer_checks import resolve_year_followup
    turns = [{'sql': "SELECT SUM(amount) FROM orders WHERE year=2024 AND city='上海'"}, {'sql': "SELECT SUM(amount) FROM orders WHERE year=2024 AND city='杭州'"}]
    text = resolve_year_followup('改成2023年呢？', turns)
    assert '杭州' in text and '上海' not in text and '2024' not in text
    assert resolve_year_followup('新问题：改成2023年呢？', turns) == '新问题：改成2023年呢？'
    assert resolve_year_followup('改成2023年呢？', []) == '改成2023年呢？'
    assert resolve_year_followup('改成2023年呢？', [{**turns[-1], 'sql_truncated': True}]) == '改成2023年呢？'
    assert "'2023'" in resolve_year_followup('改成2023年呢？', [{'sql': "SELECT id FROM t WHERE year='2024'"}])
