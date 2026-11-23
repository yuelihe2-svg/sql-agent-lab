"""Conservative, deterministic checks; never claim semantic correctness."""
import re


class NeedsClarification(Exception):
    """A user answer is needed before generating SQL."""


def clarification(question, turns):
    # A follow-up without an antecedent must not invent a metric or filters.
    usable = any(t.get('sql') for t in turns)
    if not usable and re.fullmatch(r"\s*(?:那.{1,30}呢|它们呢|这些呢|再按.{1,30}|按刚才.{0,30})[？?。！!]*\s*", question):
        return '你想查询什么指标、哪个时间范围？当前没有可参考的查询，请补充完整条件。'
    if re.fullmatch(r'\s*(?:请)?(?:帮我)?(?:统计|查询|看看|查一下)(?:一下)?(?:订单|销售|数据|收入)?[？?。！!]*\s*', question):
        return '请补充完整条件：要查询数量、金额还是明细？时间范围是什么？如需全部数据请明确说明。'
    return None


def result_caveat(feedback):
    if feedback.get('status') != 'ok':
        return ''
    if feedback.get('has_more') or feedback.get('truncated'):
        return '当前仅显示部分结果，不能将展示行数当作总数。'
    rows = feedback.get('preview_rows')
    if rows == []:
        return '没有匹配记录；这不等同于统计值为零，请核对筛选条件。'
    if rows and all(all(cell is None for cell in row) for row in rows):
        return '结果为空值，不代表零；可能没有符合条件的数据或字段缺失。'
    return ''


def query_basis(sql):
    """Describe executed SQL, never infer that it matches the user's intent."""
    from sqlglot import exp, parse_one
    from sqlglot.errors import SqlglotError
    try:
        tree = parse_one(sql, read='sqlite')
        if not isinstance(tree, exp.Select) or list(tree.find_all(exp.Join)) or len(list(tree.find_all(exp.Select))) != 1:
            return '本次为复杂查询，请展开 SQL 核对关联关系与统计口径。'
        projections = '、'.join(e.sql(dialect='sqlite') for e in tree.expressions)
        where = tree.args.get('where')
        group = tree.args.get('group')
        parts = ['统计表达式：' + projections, '筛选条件：' + (where.this.sql(dialect='sqlite') if where else '无（全部记录）')]
        if group:
            parts.append('分组：' + group.sql(dialect='sqlite'))
        text = '；'.join(parts)
        if len(text) > 800:
            return '查询条件较长，请展开 SQL 查看完整口径。'
        return '本次 SQL 使用的口径（请核对）：' + text
    except (SqlglotError, ValueError, TypeError):
        return '未能提取查询口径，请展开 SQL 核对。'


def resolve_year_followup(question, turns):
    """Resolve only an explicit year replacement against the latest successful SQL."""
    match = re.fullmatch(r'\s*(?:改成|换成|改为)([12][0-9]{3})年(?:呢)?[？?。]*\s*', question)
    if not match or not turns:
        return question
    sql = turns[-1].get('sql')
    if not sql:
        return question
    from sqlglot import exp, parse_one
    from sqlglot.errors import SqlglotError
    try:
        tree = parse_one(sql, read='sqlite')
        if not isinstance(tree, exp.Select) or len(list(tree.find_all(exp.Select))) != 1 or list(tree.find_all(exp.Join)):
            return question
        where = tree.args.get('where')
        predicates = list(where.find_all(exp.EQ)) if where else []
        years = [p for p in predicates if isinstance(p.this, exp.Column) and p.this.name.lower() == 'year' and isinstance(p.expression, exp.Literal)]
        if len(years) != 1:
            return question
        years[0].set('expression', exp.Literal.number(match[1]))
        # This is an explicit interpretation, not a reference answer or executable shortcut.
        return question + '\n本轮仅修改最近一次查询的年份，其余条件保持。统计表达式：' + ', '.join(e.sql(dialect='sqlite') for e in tree.expressions) + '；筛选条件：' + where.this.sql(dialect='sqlite')
    except (SqlglotError, ValueError):
        return question
