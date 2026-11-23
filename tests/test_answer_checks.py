from sql_agent_lab.workspace.answer_checks import clarification, result_caveat


def test_missing_antecedent():
    assert clarification('那杭州呢？', [])
    assert clarification('那杭州呢？', [{'sql': 'SELECT 1'}]) is None
    assert clarification('统计2024年杭州订单金额', []) is None


def test_no_false_zero_or_total():
    assert '零' in result_caveat({'status': 'ok', 'preview_rows': []})
    assert '空值' in result_caveat({'status': 'ok', 'preview_rows': [[None]]})
    assert '总数' in result_caveat({'status': 'ok', 'has_more': True})
    assert not result_caveat({'status': 'ok', 'preview_rows': [[0]]})


def test_query_basis_explains_top_groups():
    from sql_agent_lab.workspace.answer_checks import query_basis
    basis = query_basis('SELECT city, SUM(amount) FROM orders GROUP BY city HAVING SUM(amount)>5 '
                        'ORDER BY SUM(amount) DESC LIMIT 10 OFFSET 2')
    assert '全部记录' not in basis
    for part in ('GROUP BY', 'HAVING', 'ORDER BY', 'DESC', 'LIMIT 10', 'OFFSET 2'):
        assert part in basis
