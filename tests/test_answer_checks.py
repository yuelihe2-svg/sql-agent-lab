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


def test_year_followup_preserves_group_order_and_limit():
    from sql_agent_lab.workspace.answer_checks import resolve_year_followup
    sql = "SELECT city, SUM(amount) FROM orders WHERE year='2024' GROUP BY city HAVING SUM(amount)>5 ORDER BY SUM(amount) DESC LIMIT 3 OFFSET 1"
    resolved = resolve_year_followup('改成2023年呢？', [{'sql': sql}])
    for clause in ("'2023'", 'GROUP BY', 'HAVING', 'ORDER BY', 'DESC', 'LIMIT 3', 'OFFSET 1'):
        assert clause in resolved
    assert '2024' not in resolved

def test_ambiguous_business_questions_request_the_missing_definition():
    assert '指标' in clarification('哪个城市表现最好？', [])
    assert '分母' in clarification('给我算一下订单转化率。', [])
    assert '指哪些记录' in clarification('这些一共有几笔？', [])
    assert clarification('这些一共有几笔？', [{'sql': 'SELECT 1'}]) is None
    assert clarification('按已付款订单数除以全部订单数计算转化率。', []) is None
