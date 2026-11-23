"""Check two narrowly defined Chinese requests against the current SQLite schema."""
import re

from sqlglot import exp, parse
from sqlglot.errors import SqlglotError


_PAID_TOTAL = re.compile(
    r"(?:请)?(?:统计|计算)\s*([12][0-9]{3})\s*年\s*已付款订单(?:的)?总金额"
    r"[。；;，,]\s*已付款指\s*status\s*=\s*paid[。？?！!]?")
_CATEGORY_COUNT = re.compile(r"(?:请)?按商品分类(?:统计|计算)订单(?:数|数量)[。？?！!]?")


def initial_intent(question):
    text = re.sub(r"^新问题[：:，,]\s*", "", question.strip())
    if match := _PAID_TOTAL.fullmatch(text):
        return "paid_total", match[1]
    if _CATEGORY_COUNT.fullmatch(text):
        return "category_count", None
    return None


def supported_schema(intent, schema):
    columns = {table.lower(): {name.lower(): spec for name, spec in data["columns"].items()}
               for table, data in schema.items()}
    orders = columns.get("orders", {})
    if intent == "paid_total":
        return {"year", "status", "amount"} <= orders.keys()
    primary_key = {name for name, spec in orders.items() if spec.get("pk")}
    return primary_key == {"id"} and {"order_id", "category"} <= columns.get("order_items", {}).keys()


def _plain_select(sql, clauses):
    statements = parse(sql, read="sqlite")
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return None
    tree = statements[0]
    allowed = {"expressions", "from_", *clauses}
    if any(value and key not in allowed for key, value in tree.args.items()):
        return None
    return tree


def _table(node, name):
    return (isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier)
            and node.name.lower() == name and not node.db and not node.catalog)


def _column(node, name, table, *, qualified=False):
    return (isinstance(node, exp.Column) and node.name.lower() == name
            and not node.db and not node.catalog
            and (node.table.lower() == table.alias_or_name.lower() if node.table else not qualified))


def _unwrap(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _conjuncts(node):
    node = _unwrap(node)
    if isinstance(node, exp.And):
        yield from _conjuncts(node.left)
        yield from _conjuncts(node.right)
    else:
        yield node


def _equals(node, column, table, value, *, text=False):
    if not isinstance(node, exp.EQ):
        return False
    return any(_column(left, column, table) and isinstance(right, exp.Literal)
               and (right.is_string or not text) and str(right.this) == value
               for left, right in ((node.left, node.right), (node.right, node.left)))


def _paid_total(sql, year):
    tree = _plain_select(sql, {"where"})
    if tree is None or len(tree.expressions) != 1 or not tree.args.get("from_"):
        return False
    table = tree.args["from_"].this
    total = tree.expressions[0].unalias()
    where = tree.args.get("where")
    if not (_table(table, "orders") and isinstance(total, exp.Sum)
            and _column(total.this, "amount", table) and where):
        return False
    filters = list(_conjuncts(where.this))
    return (len(filters) == 2 and any(_equals(p, "year", table, year) for p in filters)
            and any(_equals(p, "status", table, "paid", text=True) for p in filters))


def _category_count(sql):
    tree = _plain_select(sql, {"joins", "group"})
    if tree is None or len(tree.expressions) != 2 or not tree.args.get("from_"):
        return False
    joins = tree.args.get("joins") or []
    group = tree.args.get("group")
    if len(joins) != 1 or not group or len(group.expressions) != 1 or group.args.keys() - {"expressions"}:
        return False
    join = joins[0]
    if join.side or join.kind not in ("", "INNER") or join.args.get("method") or join.args.get("using"):
        return False
    orders, items = tree.args["from_"].this, join.this
    if _table(orders, "order_items"):
        orders, items = items, orders
    if not (_table(orders, "orders") and _table(items, "order_items")):
        return False
    on = _unwrap(join.args.get("on"))
    if not isinstance(on, exp.EQ) or not any(
            _column(left, "id", orders, qualified=True) and _column(right, "order_id", items, qualified=True)
            for left, right in ((on.left, on.right), (on.right, on.left))):
        return False
    category, count = (projection.unalias() for projection in tree.expressions)
    return (_column(category, "category", items)
            and _column(group.expressions[0], "category", items)
            and isinstance(count, exp.Count) and isinstance(count.this, exp.Distinct)
            and len(count.this.expressions) == 1
            and _column(count.this.expressions[0], "id", orders, qualified=True))


def matches_initial_sql(intent, year, sql):
    if not isinstance(sql, str):
        return False
    try:
        return _paid_total(sql, year) if intent == "paid_total" else _category_count(sql)
    except (SqlglotError, ValueError, RecursionError):
        return False
