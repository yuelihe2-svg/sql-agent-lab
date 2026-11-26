import pytest
from sql_agent_lab.workspace.answer_checks import NeedsClarification
from sql_agent_lab.workspace.query_state import QueryState, resolve_filter_followup
from sql_agent_lab.workspace.conversation_context import build_conversation_context

SQL = "SELECT city, SUM(amount) FROM orders WHERE year=2024 AND city='上海' AND status='paid' GROUP BY city HAVING SUM(amount)>5 ORDER BY SUM(amount) DESC LIMIT 3 OFFSET 1"


def test_successive_updates_preserve_every_other_clause():
    state = QueryState(SQL)
    city = state.replace_filter('city', '杭州')
    year = QueryState(city).replace_filter('year', '2023')
    status = QueryState(year).replace_filter('status', 'cancelled')
    assert state.sql == SQL
    for sql, value in [(city, "city = '杭州'"), (year, 'year = 2023'), (status, "status = 'cancelled'")]:
        assert value in sql
        for clause in ('SUM(amount)', 'GROUP BY city', 'HAVING', 'ORDER BY', 'DESC', 'LIMIT 3', 'OFFSET 1'):
            assert clause in sql
    assert "city = '杭州'" in status and 'year = 2023' in status
    assert "status = 'paid'" in year


@pytest.mark.parametrize('question, expected', [('改成2023年呢？', 'year = 2023'), ('城市改成杭州', "city = '杭州'"), ('状态改成cancelled', "status = 'cancelled'")])
def test_explicit_change(question, expected):
    text, _ = resolve_filter_followup(question, [{'sql': SQL}])
    assert text.startswith(question)
    assert expected in text
    assert '不代表答案正确' in text


@pytest.mark.parametrize('sql', ["SELECT 1", "SELECT * FROM orders WHERE year=2024 OR city='上海'", "SELECT * FROM orders WHERE year=2024 AND year=2023", "SELECT * FROM orders WHERE year BETWEEN 2023 AND 2024", "SELECT * FROM orders JOIN cities ON orders.city=cities.name WHERE year=2024"])
def test_unsafe_interpretations_require_clarification(sql):
    with pytest.raises(NeedsClarification):
        resolve_filter_followup('改成2023年', [{'sql': sql}])


@pytest.mark.parametrize('last', [{}, {'sql': SQL, 'sql_truncated': True}, {'sql': SQL, 'result': {'execution_status': 'failed'}}])
def test_never_revive_old_query(last):
    with pytest.raises(NeedsClarification):
        resolve_filter_followup('改成2023年', [{'sql': SQL}, last])


def test_ambiguous_change_and_new_topic():
    for question in ('改成杭州', '年份改成去年'):
        with pytest.raises(NeedsClarification):
            resolve_filter_followup(question, [{'sql': SQL}])
    question = '新问题，统计所有订单数量'
    assert resolve_filter_followup(question, [{'sql': SQL}]) == (question, None)


@pytest.mark.parametrize('conversation, source', [('other', 'shop'), ('chat', 'archive')])
def test_scoped_state_does_not_cross_boundaries(conversation, source):
    context = build_conversation_context(conversation_id='chat', datasource_id='shop', question='改成2023年', history=[{'conversation_id': conversation, 'datasource_id': source, 'role': 'user', 'body': '统计订单', 'state': 'completed', 'execution_status': 'ok', 'sql': SQL}])
    with pytest.raises(NeedsClarification):
        resolve_filter_followup('改成2023年', context['turns'])


def test_chained_conditions_against_offline_database():
    import sqlite3
    from contextlib import closing
    sql = "SELECT SUM(amount) FROM orders WHERE year=2024 AND city='上海' AND status='paid'"
    with closing(sqlite3.connect(':memory:')) as conn:
        conn.execute('CREATE TABLE orders(year INTEGER, city TEXT, status TEXT, amount INTEGER)')
        conn.executemany('INSERT INTO orders VALUES (?, ?, ?, ?)', [
            (2024, '上海', 'paid', 300), (2024, '杭州', 'paid', 400),
            (2023, '杭州', 'paid', 800), (2023, '杭州', 'cancelled', 900)])
        assert conn.execute(sql).fetchone() == (300,)
        for column, value, expected in [('city', '杭州', 400), ('year', '2023', 800), ('status', 'cancelled', 900)]:
            sql = QueryState(sql).replace_filter(column, value)
            assert conn.execute(sql).fetchone() == (expected,)


def test_candidate_comparison_keeps_literal_values_and_requires_one_statement():
    from sql_agent_lab.workspace.query_state import matches_followup_sql
    expected = "SELECT COUNT(*) FROM orders WHERE city = 'New  York' AND year = 2024"
    assert matches_followup_sql("select count(*) from orders where city='New  York' AND year=2024;", expected)
    assert not matches_followup_sql(expected.replace('New  York', 'New York'), expected)
    assert not matches_followup_sql(expected + '; SELECT 1', expected)
    assert not matches_followup_sql(expected.replace(' AND ', ' OR '), expected)

def test_natural_filter_replacements_use_scoped_city_and_explicit_status_code():
    turns = [{'sql': SQL}]
    city_sql = resolve_filter_followup('那杭州呢？', turns, lookup_city=lambda t, v: (t, v) == ('orders', '杭州'))[1]
    assert city_sql == QueryState(SQL).replace_filter('city', '杭州')
    assert resolve_filter_followup('杭州改成2023年呢？', [{'sql': city_sql}])[1] == QueryState(city_sql).replace_filter('year', '2023')
    assert resolve_filter_followup('如果状态改为等待付款（pending），其他条件不变呢？', turns)[1] == QueryState(SQL).replace_filter('status', 'pending')
    assert resolve_filter_followup('那去年呢？', turns, lookup_city=lambda t, v: False)[1] is None
    assert resolve_filter_followup('杭州改成2023年呢？', turns)[1] is None
