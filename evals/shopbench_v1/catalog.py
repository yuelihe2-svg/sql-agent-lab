"""ShopBench questions and gold SQL. Gold SQL is materialized outside the model-visible cases."""

from __future__ import annotations

from pathlib import Path
import json

OCT = ("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z")
SEP = ("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z")
DEC = ("2026-12-01T00:00:00Z", "2027-01-01T00:00:00Z")
ROOT = Path(__file__).resolve().parents[1]


def ctx(metric, source="shop_a", start=None, end=None, filters=None, group_by=None):
    return {
        "metric": metric,
        "date_range": None if start is None else [start, end],
        "filters": list(filters or []),
        "group_by": list(group_by or []),
        "source_id": source,
    }


def captured(start, end) -> str:
    return f"""
SELECT COALESCE(SUM(amount_cents), 0) AS captured_cents
FROM payments
WHERE status = 'succeeded'
  AND paid_at >= '{start}'
  AND paid_at < '{end}'
""".strip()


def refunded(start, end) -> str:
    return f"""
SELECT COALESCE(SUM(amount_cents), 0) AS refunded_cents
FROM refunds
WHERE status = 'completed'
  AND completed_at >= '{start}'
  AND completed_at < '{end}'
""".strip()


def net_total(start, end, *, city=None, cities=None, method=None) -> str:
    pay_filter = [f"p.status = 'succeeded'", f"p.paid_at >= '{start}'", f"p.paid_at < '{end}'"]
    ref_filter = [f"r.status = 'completed'", f"r.completed_at >= '{start}'", f"r.completed_at < '{end}'"]
    pay_join = ""
    ref_join = ""
    if city or cities or method:
        pay_join = "JOIN orders o ON o.order_id = p.order_id JOIN customers c ON c.customer_id = o.customer_id"
        ref_join = (
            "JOIN payments p ON p.payment_id = r.payment_id "
            "JOIN orders o ON o.order_id = p.order_id "
            "JOIN customers c ON c.customer_id = o.customer_id"
        )
    else:
        ref_join = ""
    if city:
        pay_filter.append(f"c.city = '{city}'")
        ref_filter.append(f"c.city = '{city}'")
    if cities:
        quoted = ", ".join(f"'{item}'" for item in cities)
        pay_filter.append(f"c.city IN ({quoted})")
        ref_filter.append(f"c.city IN ({quoted})")
    if method:
        pay_filter.append(f"p.payment_method = '{method}'")
        ref_filter.append(f"p.payment_method = '{method}'")
    if not (city or cities or method):
        returned_from = "refunds r"
    else:
        returned_from = f"refunds r {ref_join}"
    return f"""
WITH captured AS (
    SELECT COALESCE(SUM(p.amount_cents), 0) AS amount
    FROM payments p
    {pay_join}
    WHERE {' AND '.join(pay_filter)}
),
returned AS (
    SELECT COALESCE(SUM(r.amount_cents), 0) AS amount
    FROM {returned_from}
    WHERE {' AND '.join(ref_filter)}
)
SELECT captured.amount - returned.amount AS net_receipts_cents
FROM captured CROSS JOIN returned
""".strip()


def _net_scalar(start, end) -> str:
    return (
        f"(SELECT COALESCE(SUM(amount_cents), 0) FROM payments "
        f"WHERE status = 'succeeded' AND paid_at >= '{start}' AND paid_at < '{end}')"
        f" - (SELECT COALESCE(SUM(amount_cents), 0) FROM refunds "
        f"WHERE status = 'completed' AND completed_at >= '{start}' AND completed_at < '{end}')"
    )


def net_by_city(start, end, cities=None) -> str:
    having = ""
    if cities:
        quoted = ", ".join(f"'{item}'" for item in cities)
        having = f"WHERE cities.city IN ({quoted})"
    return f"""
WITH captured AS (
    SELECT c.city AS city, SUM(p.amount_cents) AS amount
    FROM payments p
    JOIN orders o ON o.order_id = p.order_id
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE p.status = 'succeeded'
      AND p.paid_at >= '{start}' AND p.paid_at < '{end}'
    GROUP BY c.city
),
returned AS (
    SELECT c.city AS city, SUM(r.amount_cents) AS amount
    FROM refunds r
    JOIN payments p ON p.payment_id = r.payment_id
    JOIN orders o ON o.order_id = p.order_id
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE r.status = 'completed'
      AND r.completed_at >= '{start}' AND r.completed_at < '{end}'
    GROUP BY c.city
),
cities AS (
    SELECT DISTINCT city FROM customers
)
SELECT cities.city,
       COALESCE(captured.amount, 0) - COALESCE(returned.amount, 0) AS net_receipts_cents
FROM cities
LEFT JOIN captured ON captured.city = cities.city
LEFT JOIN returned ON returned.city = cities.city
{having}
""".strip()


def refunds_by_city(start, end) -> str:
    return f"""
SELECT c.city, COALESCE(SUM(r.amount_cents), 0) AS refunded_cents
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id
LEFT JOIN payments p ON p.order_id = o.order_id
LEFT JOIN refunds r ON r.payment_id = p.payment_id
  AND r.status = 'completed'
  AND r.completed_at >= '{start}'
  AND r.completed_at < '{end}'
GROUP BY c.city
""".strip()


def merchandise_by_category(start, end) -> str:
    return f"""
SELECT c.category_name,
       COALESCE(m.amount, 0) AS merchandise_cents
FROM categories c
LEFT JOIN (
    SELECT p.category_id AS category_id,
           SUM(oi.quantity * oi.unit_price_cents - oi.discount_cents) AS amount
    FROM order_items oi
    JOIN orders o ON o.order_id = oi.order_id
    JOIN products p ON p.product_id = oi.product_id
    WHERE o.ordered_at >= '{start}' AND o.ordered_at < '{end}'
    GROUP BY p.category_id
) m ON m.category_id = c.category_id
""".strip()


def _orders_by_city(start, end, city, paid_only) -> str:
    extra = []
    if start:
        extra.append(f"o.ordered_at >= '{start}'")
        extra.append(f"o.ordered_at < '{end}'")
    if paid_only:
        extra.append("o.status != 'cancelled'")
        extra.append(
            "EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'succeeded')"
        )
    extra_sql = (" AND " + " AND ".join(extra)) if extra else ""
    city_sql = f"WHERE c.city = '{city}'" if city else ""
    # The city filter must not turn the join into an inner join for other cities.
    if city:
        return f"""
SELECT COUNT(DISTINCT o.order_id) AS order_count
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id
{extra_sql.replace(" AND ", " AND ", 1) if False else ""}
WHERE c.city = '{city}' {("AND " + " AND ".join(extra)) if extra else ""}
""".strip()
    return f"""
SELECT c.city, COUNT(DISTINCT o.order_id) AS order_count
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id {("AND " + " AND ".join(extra)) if extra else ""}
GROUP BY c.city
""".strip()


def partial_orders_cte() -> str:
    return """
partial_orders AS (
    SELECT p.order_id
    FROM payments p
    JOIN refunds r ON r.payment_id = p.payment_id AND r.status = 'completed'
    WHERE p.status = 'succeeded'
    GROUP BY p.order_id
    HAVING SUM(r.amount_cents) > 0
       AND SUM(r.amount_cents) < (
           SELECT COALESCE(SUM(p2.amount_cents), 0)
           FROM payments p2
           WHERE p2.order_id = p.order_id AND p2.status = 'succeeded'
       )
)
""".strip()


def single_cases() -> list[dict]:
    rows = [
        ("S01", "数据库共有多少客户？", "SELECT COUNT(*) AS customer_count FROM customers", "count", False, None, ["customer"]),
        ("S02", "有多少活跃客户？", "SELECT COUNT(*) AS active_customer_count FROM customers WHERE is_active = 1", "count", False, None, ["customer"]),
        ("S03", "上海有多少客户？", "SELECT COUNT(*) AS shanghai_customer_count FROM customers WHERE city = '上海'", "count", False, None, ["customer", "city"]),
        ("S04", "有多少种商品？", "SELECT COUNT(*) AS product_count FROM products", "count", False, None, ["product"]),
        ("S05", "每个商品类别有多少商品？", """
SELECT c.category_name, COUNT(p.product_id) AS product_count
FROM categories c
LEFT JOIN products p ON p.category_id = c.category_id
GROUP BY c.category_id, c.category_name
""".strip(), "rows", False, None, ["product", "category"]),
        ("S06", "2026年10月创建了多少订单？", f"""
SELECT COUNT(DISTINCT order_id) AS order_count
FROM orders
WHERE ordered_at >= '{OCT[0]}' AND ordered_at < '{OCT[1]}'
""".strip(), "count", False, None, ["order", "time"]),
        ("S07", "有多少订单处于 cancelled 状态？", "SELECT COUNT(*) AS cancelled_order_count FROM orders WHERE status = 'cancelled'", "count", False, None, ["order"]),
        ("S08", "2026年10月注册了多少客户？", f"""
SELECT COUNT(*) AS signup_count
FROM customers
WHERE signup_at >= '{OCT[0]}' AND signup_at < '{OCT[1]}'
""".strip(), "count", False, None, ["customer", "time"]),
        ("S09", "2026年10月每天创建了多少订单？", f"""
SELECT substr(ordered_at, 1, 10) AS order_date, COUNT(DISTINCT order_id) AS order_count
FROM orders
WHERE ordered_at >= '{OCT[0]}' AND ordered_at < '{OCT[1]}'
GROUP BY substr(ordered_at, 1, 10)
""".strip(), "rows", False, None, ["order", "time"]),
        ("S10", "各个城市有多少客户？", "SELECT city, COUNT(*) AS customer_count FROM customers GROUP BY city", "rows", False, None, ["customer", "city"]),
        ("S11", "每个商品类别的平均当前标价是多少？", """
SELECT c.category_name, AVG(p.list_price_cents) AS avg_list_price_cents
FROM categories c
JOIN products p ON p.category_id = c.category_id
GROUP BY c.category_id, c.category_name
""".strip(), "real", False, 1e-6, ["product", "category", "price"]),
        ("S12", "订单数量最多的五位客户是谁？并列时按姓名、客户编号升序取前五名。", """
SELECT cu.customer_id, cu.full_name, COUNT(DISTINCT o.order_id) AS order_count
FROM customers cu
JOIN orders o ON o.customer_id = cu.customer_id
GROUP BY cu.customer_id, cu.full_name
ORDER BY order_count DESC, cu.full_name ASC, cu.customer_id ASC
LIMIT 5
""".strip(), "rows", True, None, ["customer", "order", "rank"]),
        ("S13", "每个月有多少笔成功支付记录？", """
SELECT substr(paid_at, 1, 7) AS paid_month, COUNT(*) AS payment_count
FROM payments
WHERE status = 'succeeded'
GROUP BY substr(paid_at, 1, 7)
""".strip(), "rows", False, None, ["payment", "time"]),
        ("S14", "2026年10月成功收款总额是多少？", captured(*OCT), "money_cents", False, None, ["payment", "time"]),
        ("S15", "2026年10月已完成退款总额是多少？", refunded(*OCT), "money_cents", False, None, ["refund", "time"]),
        ("S16", "2026年10月净收款额是多少？", net_total(*OCT), "money_cents", False, None, ["aggregate", "payment", "refund"]),
        ("S17", "每个客户一共创建了多少订单，包括从未下单的客户？", """
SELECT cu.customer_id, cu.full_name, COUNT(DISTINCT o.order_id) AS order_count
FROM customers cu
LEFT JOIN orders o ON o.customer_id = cu.customer_id
GROUP BY cu.customer_id, cu.full_name
""".strip(), "rows", False, None, ["customer", "order", "join"]),
        ("S18", "2026年10月每个商品类别的订单明细成交金额是多少？", merchandise_by_category(*OCT), "rows", False, None, ["merchandise", "category", "time"]),
        ("S19", "购买订单数最多的五个城市是哪些？并列时按城市名升序取前五名。购买订单不含取消订单，城市用客户注册城市。", """
SELECT cu.city, COUNT(DISTINCT o.order_id) AS order_count
FROM orders o
JOIN customers cu ON cu.customer_id = o.customer_id
WHERE o.status != 'cancelled'
GROUP BY cu.city
ORDER BY order_count DESC, cu.city ASC
LIMIT 5
""".strip(), "rows", True, None, ["order", "city", "rank"]),
        ("S20", "哪些客户从未创建过订单？", """
SELECT cu.customer_id, cu.full_name
FROM customers cu
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = cu.customer_id)
""".strip(), "rows", False, None, ["customer", "order", "exists"]),
        ("S21", "哪些产品从未出现在订单明细中？", """
SELECT p.product_id, p.product_name
FROM products p
WHERE NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.product_id = p.product_id)
""".strip(), "rows", False, None, ["product", "exists"]),
        ("S22", "2026年10月上海客户的净收款额是多少？城市按客户注册城市。", net_total(*OCT, city="上海"), "money_cents", False, None, ["payment", "refund", "city"]),
        ("S23", "各城市的成功收款总额是多少？城市按客户注册城市。", """
WITH captured AS (
    SELECT c.city AS city, SUM(p.amount_cents) AS amount
    FROM payments p
    JOIN orders o ON o.order_id = p.order_id
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE p.status = 'succeeded'
    GROUP BY c.city
)
SELECT cities.city, COALESCE(captured.amount, 0) AS captured_cents
FROM (SELECT DISTINCT city FROM customers) AS cities
LEFT JOIN captured ON captured.city = cities.city
""".strip(), "rows", False, None, ["payment", "city"]),
        ("S24", "哪些客户至少有一笔完成退款？", """
SELECT DISTINCT c.customer_id, c.full_name
FROM customers c
JOIN orders o ON o.customer_id = c.customer_id
JOIN payments p ON p.order_id = o.order_id
JOIN refunds r ON r.payment_id = p.payment_id
WHERE r.status = 'completed'
""".strip(), "rows", False, None, ["customer", "refund"]),
        ("S25", "一张订单多次成功支付时，总收款是这些成功支付金额之和。请列出所有有两笔或更多成功支付的订单及合计。", """
SELECT o.order_id, SUM(p.amount_cents) AS captured_cents
FROM orders o
JOIN payments p ON p.order_id = o.order_id AND p.status = 'succeeded'
GROUP BY o.order_id
HAVING COUNT(*) >= 2
""".strip(), "rows", False, None, ["payment", "order"]),
        ("S26", "列出所有有两笔或更多完成退款的支付及退款合计。", """
SELECT p.payment_id, SUM(r.amount_cents) AS refunded_cents
FROM payments p
JOIN refunds r ON r.payment_id = p.payment_id AND r.status = 'completed'
GROUP BY p.payment_id
HAVING COUNT(*) >= 2
""".strip(), "rows", False, None, ["refund", "payment"]),
        ("S27", "2026年10月退款完成金额最高的五位客户是谁？并列时按姓名、客户编号升序。", f"""
SELECT c.customer_id, c.full_name, SUM(r.amount_cents) AS refunded_cents
FROM refunds r
JOIN payments p ON p.payment_id = r.payment_id
JOIN orders o ON o.order_id = p.order_id
JOIN customers c ON c.customer_id = o.customer_id
WHERE r.status = 'completed'
  AND r.completed_at >= '{OCT[0]}' AND r.completed_at < '{OCT[1]}'
GROUP BY c.customer_id, c.full_name
ORDER BY refunded_cents DESC, c.full_name ASC, c.customer_id ASC
LIMIT 5
""".strip(), "rows", True, None, ["refund", "customer", "rank"]),
        ("S28", "2026年10月净收款额最高的五个城市是哪些？并列时按城市名升序。", net_by_city(*OCT) + " ORDER BY net_receipts_cents DESC, cities.city ASC LIMIT 5", "rows", True, None, ["payment", "refund", "city", "rank"]),
        ("S29", "2026年9月和10月的净收款额分别是多少？", f"""
WITH sep AS (
    SELECT COALESCE((SELECT SUM(amount_cents) FROM payments WHERE status = 'succeeded' AND paid_at >= '{SEP[0]}' AND paid_at < '{SEP[1]}'), 0)
         - COALESCE((SELECT SUM(amount_cents) FROM refunds WHERE status = 'completed' AND completed_at >= '{SEP[0]}' AND completed_at < '{SEP[1]}'), 0)
         AS net_receipts_cents
),
oct AS (
    SELECT COALESCE((SELECT SUM(amount_cents) FROM payments WHERE status = 'succeeded' AND paid_at >= '{OCT[0]}' AND paid_at < '{OCT[1]}'), 0)
         - COALESCE((SELECT SUM(amount_cents) FROM refunds WHERE status = 'completed' AND completed_at >= '{OCT[0]}' AND completed_at < '{OCT[1]}'), 0)
         AS net_receipts_cents
)
SELECT '2026-09' AS month, net_receipts_cents FROM sep
UNION ALL
SELECT '2026-10' AS month, net_receipts_cents FROM oct
""".strip(), "rows", False, None, ["payment", "refund", "time"]),
        ("S30", "2026年10月与9月相比，净收款额增长了多少？增长额是10月净收款减去9月净收款，单位为分。", f"""
WITH sep AS (
    SELECT COALESCE((SELECT SUM(amount_cents) FROM payments WHERE status = 'succeeded' AND paid_at >= '{SEP[0]}' AND paid_at < '{SEP[1]}'), 0)
         - COALESCE((SELECT SUM(amount_cents) FROM refunds WHERE status = 'completed' AND completed_at >= '{SEP[0]}' AND completed_at < '{SEP[1]}'), 0)
         AS net_receipts_cents
),
oct AS (
    SELECT COALESCE((SELECT SUM(amount_cents) FROM payments WHERE status = 'succeeded' AND paid_at >= '{OCT[0]}' AND paid_at < '{OCT[1]}'), 0)
         - COALESCE((SELECT SUM(amount_cents) FROM refunds WHERE status = 'completed' AND completed_at >= '{OCT[0]}' AND completed_at < '{OCT[1]}'), 0)
         AS net_receipts_cents
)
SELECT oct.net_receipts_cents - sep.net_receipts_cents AS growth_cents
FROM sep CROSS JOIN oct
""".strip(), "money_cents", False, None, ["payment", "refund", "time"]),
        ("S31", "2026年10月各城市净收款额与9月相比的变化是多少？变化等于10月减去9月，城市用客户注册城市。", f"""
WITH sep AS (
    SELECT city, net_receipts_cents FROM ({net_by_city(*SEP)})
),
oct AS (
    SELECT city, net_receipts_cents FROM ({net_by_city(*OCT)})
)
SELECT oct.city, oct.net_receipts_cents - sep.net_receipts_cents AS net_change_cents
FROM oct
JOIN sep ON sep.city = oct.city
""".strip(), "rows", False, None, ["payment", "refund", "city", "time"]),
        ("S32", "2026年10月每个城市的有效支付订单数是多少？有效支付订单是非取消且至少有一笔成功支付的订单，按订单创建时间归属月份。", f"""
SELECT c.city, COUNT(DISTINCT o.order_id) AS paid_order_count
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id
  AND o.status != 'cancelled'
  AND o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
  AND EXISTS (
      SELECT 1 FROM payments p
      WHERE p.order_id = o.order_id AND p.status = 'succeeded'
  )
GROUP BY c.city
""".strip(), "rows", False, None, ["order", "payment", "city"]),
        ("S33", "有多少客户没有填写邮箱？", "SELECT COUNT(*) AS missing_email_count FROM customers WHERE email IS NULL", "count", False, None, ["customer", "null"]),
        ("S34", "有多少订单没有成功支付？", """
SELECT COUNT(*) AS unpaid_order_count
FROM orders o
WHERE NOT EXISTS (
    SELECT 1 FROM payments p
    WHERE p.order_id = o.order_id AND p.status = 'succeeded'
)
""".strip(), "count", False, None, ["order", "payment"]),
        ("S35", "有多少订单发生了部分退款？部分退款指已完成退款大于0且小于该订单成功收款。", f"""
WITH {partial_orders_cte()}
SELECT COUNT(*) AS partial_refund_order_count FROM partial_orders
""".strip(), "count", False, None, ["refund", "order"]),
        ("S36", "有多少订单已经全部退款？全部退款指成功收款大于0且已完成退款等于成功收款。", """
WITH pay AS (
    SELECT order_id, SUM(amount_cents) AS captured
    FROM payments WHERE status = 'succeeded'
    GROUP BY order_id
),
ref AS (
    SELECT p.order_id AS order_id, SUM(r.amount_cents) AS refunded
    FROM refunds r
    JOIN payments p ON p.payment_id = r.payment_id
    WHERE r.status = 'completed'
    GROUP BY p.order_id
)
SELECT COUNT(*) AS full_refund_order_count
FROM pay
JOIN ref ON ref.order_id = pay.order_id
WHERE pay.captured > 0 AND ref.refunded = pay.captured
""".strip(), "count", False, None, ["refund", "order"]),
        ("S37", "2026年12月的成功收款总额是多少？没有记录时回答0。", captured(*DEC), "money_cents", False, None, ["payment", "empty"]),
        ("S38", "有哪些客户创建了订单，但名下没有任何成功支付记录？", """
SELECT c.customer_id, c.full_name
FROM customers c
WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id)
  AND NOT EXISTS (
      SELECT 1
      FROM orders o
      JOIN payments p ON p.order_id = o.order_id
      WHERE o.customer_id = c.customer_id AND p.status = 'succeeded'
  )
""".strip(), "rows", False, None, ["customer", "payment"]),
        ("S39", "哪些商品当前标价与历史成交单价不同？", """
SELECT DISTINCT p.product_id, p.product_name
FROM products p
JOIN order_items oi ON oi.product_id = p.product_id
WHERE oi.unit_price_cents != p.list_price_cents
""".strip(), "rows", False, None, ["product", "price"]),
        ("S40", "找出2026年10月成功收款额最高的客户，给出客户编号、姓名和金额。并列时按姓名升序、客户编号升序只取第一名。", f"""
SELECT c.customer_id, c.full_name, SUM(p.amount_cents) AS captured_cents
FROM payments p
JOIN orders o ON o.order_id = p.order_id
JOIN customers c ON c.customer_id = o.customer_id
WHERE p.status = 'succeeded'
  AND p.paid_at >= '{OCT[0]}' AND p.paid_at < '{OCT[1]}'
GROUP BY c.customer_id, c.full_name
ORDER BY captured_cents DESC, c.full_name ASC, c.customer_id ASC
LIMIT 1
""".strip(), "rows", True, None, ["payment", "customer", "rank"]),
    ]
    cases = []
    for case_id, question, sql, answer_type, ordered, tolerance, tags in rows:
        number = int(case_id[1:])
        cases.append({
            "case_id": case_id,
            "source_id": "shop_a",
            "question": question,
            "gold_id": f"G{number:02d}",
            "answer_type": answer_type,
            "order_sensitive": ordered,
            "numeric_tolerance": tolerance,
            "tags": tags,
            "sql": " ".join(sql.split()),
        })
    if len(cases) != 40:
        raise RuntimeError(f"Expected 40 single-turn cases, found {len(cases)}")
    return cases


def _turn(question, sql, answer_type, ordered, tolerance, context, status="eligible", reason=None):
    item = {
        "question": question,
        "sql": None if sql is None else " ".join(sql.split()),
        "answer_type": answer_type,
        "order_sensitive": ordered,
        "numeric_tolerance": tolerance,
        "status": status,
        "expected_context": context,
    }
    if reason:
        item["reason"] = reason
    return item


def multi_sessions() -> list[dict]:
    top_refund_city = f"""
WITH by_city AS (
    {refunds_by_city(*OCT)}
)
SELECT city, refunded_cents
FROM by_city
ORDER BY refunded_cents DESC, city ASC
LIMIT 1
""".strip()
    top_refund_compare = f"""
WITH october AS (
    {refunds_by_city(*OCT)}
),
september AS (
    {refunds_by_city(*SEP)}
),
top_city AS (
    SELECT city FROM october
    ORDER BY refunded_cents DESC, city ASC
    LIMIT 1
)
SELECT '2026-09' AS month, september.city, september.refunded_cents
FROM september
WHERE september.city = (SELECT city FROM top_city)
UNION ALL
SELECT '2026-10' AS month, october.city, october.refunded_cents
FROM october
WHERE october.city = (SELECT city FROM top_city)
""".strip()
    sessions = [
        ("M01", [
            _turn("2026年10月的净收款额是多少？", net_total(*OCT), "money_cents", False, None, ctx("net_receipts", start=OCT[0], end=OCT[1])),
            _turn("按客户城市拆开看看。", net_by_city(*OCT), "rows", False, None, ctx("net_receipts", start=OCT[0], end=OCT[1], group_by=["city"])),
            _turn("只看上海和北京。", net_by_city(*OCT, cities=("上海", "北京")), "rows", False, None, ctx("net_receipts", start=OCT[0], end=OCT[1], filters=["city IN (上海, 北京)"], group_by=["city"])),
            _turn("改成9月。", net_by_city(*SEP, cities=("上海", "北京")), "rows", False, None, ctx("net_receipts", start=SEP[0], end=SEP[1], filters=["city IN (上海, 北京)"], group_by=["city"])),
        ]),
        ("M02", [
            _turn("2026年10月创建了多少订单？", f"SELECT COUNT(DISTINCT order_id) AS order_count FROM orders WHERE ordered_at >= '{OCT[0]}' AND ordered_at < '{OCT[1]}'", "count", False, None, ctx("order_count", start=OCT[0], end=OCT[1])),
            _turn("按客户城市统计。", _orders_by_city(*OCT, None, False), "rows", False, None, ctx("order_count", start=OCT[0], end=OCT[1], group_by=["city"])),
            _turn("只看上海。", _orders_by_city(*OCT, "上海", False), "count", False, None, ctx("order_count", start=OCT[0], end=OCT[1], filters=["city = 上海"])),
            _turn("和9月比一下。", f"""
SELECT '2026-09' AS month, COUNT(DISTINCT o.order_id) AS order_count
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
WHERE c.city = '上海' AND o.ordered_at >= '{SEP[0]}' AND o.ordered_at < '{SEP[1]}'
UNION ALL
SELECT '2026-10' AS month, COUNT(DISTINCT o.order_id) AS order_count
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
WHERE c.city = '上海' AND o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
""".strip(), "rows", False, None, ctx("order_count", start=None, filters=["city = 上海"], group_by=["month"])),
        ]),
        ("M03", [
            _turn("2026年10月成功收款总额是多少？", captured(*OCT), "money_cents", False, None, ctx("captured_receipts", start=OCT[0], end=OCT[1])),
            _turn("按支付方式拆开。", f"""
WITH methods(payment_method) AS (
    SELECT 'card' UNION ALL SELECT 'alipay' UNION ALL SELECT 'wechat'
)
SELECT m.payment_method, COALESCE(SUM(p.amount_cents), 0) AS captured_cents
FROM methods m
LEFT JOIN payments p ON p.payment_method = m.payment_method
  AND p.status = 'succeeded'
  AND p.paid_at >= '{OCT[0]}' AND p.paid_at < '{OCT[1]}'
GROUP BY m.payment_method
""".strip(), "rows", False, None, ctx("captured_receipts", start=OCT[0], end=OCT[1], group_by=["payment_method"])),
            _turn("只看支付宝。", f"""
SELECT COALESCE(SUM(amount_cents), 0) AS captured_cents
FROM payments
WHERE status = 'succeeded' AND payment_method = 'alipay'
  AND paid_at >= '{OCT[0]}' AND paid_at < '{OCT[1]}'
""".strip(), "money_cents", False, None, ctx("captured_receipts", start=OCT[0], end=OCT[1], filters=["payment_method = alipay"])),
            _turn("改成9月。", f"""
SELECT COALESCE(SUM(amount_cents), 0) AS captured_cents
FROM payments
WHERE status = 'succeeded' AND payment_method = 'alipay'
  AND paid_at >= '{SEP[0]}' AND paid_at < '{SEP[1]}'
""".strip(), "money_cents", False, None, ctx("captured_receipts", start=SEP[0], end=SEP[1], filters=["payment_method = alipay"])),
        ]),
        ("M04", [
            _turn("2026年10月已完成退款总额是多少？", refunded(*OCT), "money_cents", False, None, ctx("completed_refunds", start=OCT[0], end=OCT[1])),
            _turn("按客户城市拆开。", refunds_by_city(*OCT), "rows", False, None, ctx("completed_refunds", start=OCT[0], end=OCT[1], group_by=["city"])),
            _turn("只看退款最高的那个城市。", top_refund_city, "rows", True, None, ctx("completed_refunds", start=OCT[0], end=OCT[1], filters=["city = top_october_refund_city"])),
            _turn("这个城市和上个月比呢？", top_refund_compare, "rows", False, None, ctx("completed_refunds", filters=["city = top_october_refund_city"], group_by=["month"])),
        ]),
        ("M05", [
            _turn("有哪些商品类别？", "SELECT category_id, category_name FROM categories", "rows", False, None, ctx("categories")),
            _turn("每个类别有多少商品？", """
SELECT c.category_name, COUNT(p.product_id) AS product_count
FROM categories c LEFT JOIN products p ON p.category_id = c.category_id
GROUP BY c.category_id, c.category_name
""".strip(), "rows", False, None, ctx("product_count", group_by=["category"])),
            _turn("只看商品数量最多的类别。", """
SELECT c.category_id, c.category_name, COUNT(p.product_id) AS product_count
FROM categories c JOIN products p ON p.category_id = c.category_id
GROUP BY c.category_id, c.category_name
ORDER BY product_count DESC, c.category_name ASC
LIMIT 1
""".strip(), "rows", True, None, ctx("product_count", filters=["category = most_products"])),
            _turn("列出该类里的商品。", """
WITH top_category AS (
    SELECT c.category_id
    FROM categories c JOIN products p ON p.category_id = c.category_id
    GROUP BY c.category_id, c.category_name
    ORDER BY COUNT(p.product_id) DESC, c.category_name ASC
    LIMIT 1
)
SELECT p.product_id, p.product_name
FROM products p
WHERE p.category_id = (SELECT category_id FROM top_category)
""".strip(), "rows", False, None, ctx("products", filters=["category = most_products"])),
        ]),
        ("M06", [
            _turn("2026年10月哪些商品卖得最多？按销售数量算。", f"""
SELECT p.product_id, p.product_name, SUM(oi.quantity) AS quantity_sold
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
WHERE o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
GROUP BY p.product_id, p.product_name
ORDER BY quantity_sold DESC, p.product_name ASC
""".strip(), "rows", True, None, ctx("quantity_sold", start=OCT[0], end=OCT[1], group_by=["product"])),
            _turn("只取前五名。", f"""
SELECT p.product_id, p.product_name, SUM(oi.quantity) AS quantity_sold
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
WHERE o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
GROUP BY p.product_id, p.product_name
ORDER BY quantity_sold DESC, p.product_name ASC
LIMIT 5
""".strip(), "rows", True, None, ctx("quantity_sold", start=OCT[0], end=OCT[1], filters=["top_k = 5"], group_by=["product"])),
            _turn("它们分别属于什么类别？", f"""
WITH top5 AS (
    SELECT p.product_id
    FROM order_items oi
    JOIN orders o ON o.order_id = oi.order_id
    JOIN products p ON p.product_id = oi.product_id
    WHERE o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
    GROUP BY p.product_id, p.product_name
    ORDER BY SUM(oi.quantity) DESC, p.product_name ASC
    LIMIT 5
)
SELECT p.product_id, p.product_name, c.category_name
FROM products p
JOIN categories c ON c.category_id = p.category_id
WHERE p.product_id IN (SELECT product_id FROM top5)
""".strip(), "rows", False, None, ctx("product_category", start=OCT[0], end=OCT[1], filters=["product IN top5_october_quantity"])),
            _turn("改成只看家居类在10月的销量。", f"""
SELECT p.product_id, p.product_name,
       COALESCE(SUM(CASE WHEN o.order_id IS NOT NULL THEN oi.quantity END), 0) AS quantity_sold
FROM products p
JOIN categories c ON c.category_id = p.category_id AND c.category_name = '家居'
LEFT JOIN order_items oi ON oi.product_id = p.product_id
LEFT JOIN orders o ON o.order_id = oi.order_id
  AND o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
GROUP BY p.product_id, p.product_name
""".strip(), "rows", False, None, ctx("quantity_sold", start=OCT[0], end=OCT[1], filters=["category = 家居"], group_by=["product"])),
        ]),
        ("M07", [
            _turn("上海有多少客户？", "SELECT COUNT(*) AS customer_count FROM customers WHERE city = '上海'", "count", False, None, ctx("customer_count", filters=["city = 上海"])),
            _turn("其中活跃客户有多少？", "SELECT COUNT(*) AS customer_count FROM customers WHERE city = '上海' AND is_active = 1", "count", False, None, ctx("customer_count", filters=["city = 上海", "is_active = 1"])),
            _turn("只看10月注册的。", f"SELECT COUNT(*) AS customer_count FROM customers WHERE city = '上海' AND is_active = 1 AND signup_at >= '{OCT[0]}' AND signup_at < '{OCT[1]}'", "count", False, None, ctx("customer_count", start=OCT[0], end=OCT[1], filters=["city = 上海", "is_active = 1"])),
            _turn("和北京按同样口径比一下。", f"""
SELECT city, COUNT(*) AS customer_count
FROM customers
WHERE is_active = 1 AND city IN ('上海', '北京')
  AND signup_at >= '{OCT[0]}' AND signup_at < '{OCT[1]}'
GROUP BY city
""".strip(), "rows", False, None, ctx("customer_count", start=OCT[0], end=OCT[1], filters=["city IN (上海, 北京)", "is_active = 1"], group_by=["city"])),
        ]),
        ("M08", [
            _turn("哪些客户从未创建过订单？", """
SELECT customer_id, full_name FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id)
""".strip(), "rows", False, None, ctx("customers_without_orders")),
            _turn("按城市统计这些客户有多少人。", """
SELECT c.city, COUNT(*) AS customer_count
FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id)
GROUP BY c.city
""".strip(), "rows", False, None, ctx("customers_without_orders", group_by=["city"])),
            _turn("只看北京。", """
SELECT COUNT(*) AS customer_count
FROM customers c
WHERE c.city = '北京'
  AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id)
""".strip(), "count", False, None, ctx("customers_without_orders", filters=["city = 北京"])),
            _turn("那一共有多少人？", """
SELECT COUNT(*) AS customer_count
FROM customers c
WHERE c.city = '北京'
  AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id)
""".strip(), "count", False, None, ctx("customers_without_orders", filters=["city = 北京"])),
        ]),
        ("M09", [
            _turn("有多少订单没有成功支付？", """
SELECT COUNT(*) AS unpaid_order_count FROM orders o
WHERE NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'succeeded')
""".strip(), "count", False, None, ctx("orders_without_succeeded_payment")),
            _turn("按订单创建月份统计。", """
SELECT substr(o.ordered_at, 1, 7) AS ordered_month, COUNT(*) AS unpaid_order_count
FROM orders o
WHERE NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'succeeded')
GROUP BY substr(o.ordered_at, 1, 7)
""".strip(), "rows", False, None, ctx("orders_without_succeeded_payment", group_by=["month"])),
            _turn("只看10月。", f"""
SELECT COUNT(*) AS unpaid_order_count FROM orders o
WHERE o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
  AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'succeeded')
""".strip(), "count", False, None, ctx("orders_without_succeeded_payment", start=OCT[0], end=OCT[1])),
            _turn("这些订单对应哪些客户？", f"""
SELECT DISTINCT c.customer_id, c.full_name
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
WHERE o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
  AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'succeeded')
""".strip(), "rows", False, None, ctx("customers", start=OCT[0], end=OCT[1], filters=["order has no succeeded payment"])),
        ]),
        ("M10", [
            _turn("2026年10月订单明细的成交金额一共是多少？", f"""
SELECT COALESCE(SUM(oi.quantity * oi.unit_price_cents - oi.discount_cents), 0) AS merchandise_cents
FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
WHERE o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
""".strip(), "money_cents", False, None, ctx("merchandise", start=OCT[0], end=OCT[1])),
            _turn("按商品类别拆开。", merchandise_by_category(*OCT), "rows", False, None, ctx("merchandise", start=OCT[0], end=OCT[1], group_by=["category"])),
            _turn("只看成交金额最高的前两类。", merchandise_by_category(*OCT) + " ORDER BY merchandise_cents DESC, c.category_name ASC LIMIT 2", "rows", True, None, ctx("merchandise", start=OCT[0], end=OCT[1], filters=["top_k = 2"], group_by=["category"])),
            _turn("这两类和9月比一下。", f"""
WITH monthly AS (
    SELECT c.category_name AS category_name, substr(o.ordered_at, 1, 7) AS month,
           SUM(oi.quantity * oi.unit_price_cents - oi.discount_cents) AS merchandise_cents
    FROM order_items oi
    JOIN orders o ON o.order_id = oi.order_id
    JOIN products p ON p.product_id = oi.product_id
    JOIN categories c ON c.category_id = p.category_id
    WHERE o.ordered_at >= '{SEP[0]}' AND o.ordered_at < '{OCT[1]}'
    GROUP BY c.category_name, substr(o.ordered_at, 1, 7)
),
top2 AS (
    SELECT category_name FROM monthly WHERE month = '2026-10'
    ORDER BY merchandise_cents DESC, category_name ASC
    LIMIT 2
)
SELECT month, category_name, merchandise_cents
FROM monthly
WHERE category_name IN (SELECT category_name FROM top2)
""".strip(), "rows", False, None, ctx("merchandise", filters=["category IN top2_october"], group_by=["month", "category"])),
        ]),
        ("M11", [
            _turn("有过支付记录的订单有多少？失败和待处理也算。", """
SELECT COUNT(DISTINCT o.order_id) AS order_count
FROM orders o
WHERE EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id)
""".strip(), "count", False, None, ctx("orders_with_any_payment")),
            _turn("排除取消订单，并且只保留至少有一笔成功支付的订单。", """
SELECT COUNT(DISTINCT o.order_id) AS order_count
FROM orders o
WHERE o.status != 'cancelled'
  AND EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'succeeded')
""".strip(), "count", False, None, ctx("effective_paid_orders", filters=["status != cancelled", "has succeeded payment"])),
            _turn("按客户城市统计。", """
SELECT c.city, COUNT(DISTINCT o.order_id) AS order_count
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id
  AND o.status != 'cancelled'
  AND EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'succeeded')
GROUP BY c.city
""".strip(), "rows", False, None, ctx("effective_paid_orders", filters=["status != cancelled", "has succeeded payment"], group_by=["city"])),
            _turn("改成只看10月创建的订单。", f"""
SELECT c.city, COUNT(DISTINCT o.order_id) AS order_count
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id
  AND o.status != 'cancelled'
  AND o.ordered_at >= '{OCT[0]}' AND o.ordered_at < '{OCT[1]}'
  AND EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'succeeded')
GROUP BY c.city
""".strip(), "rows", False, None, ctx("effective_paid_orders", start=OCT[0], end=OCT[1], filters=["status != cancelled", "has succeeded payment"], group_by=["city"])),
        ]),
        ("M12", [
            _turn("有多少订单发生了部分退款？", f"WITH {partial_orders_cte()} SELECT COUNT(*) AS partial_refund_order_count FROM partial_orders", "count", False, None, ctx("partial_refund_orders")),
            _turn("这些订单的已完成退款总额是多少？", f"""
WITH {partial_orders_cte()}
SELECT COALESCE(SUM(r.amount_cents), 0) AS refunded_cents
FROM partial_orders po
JOIN payments p ON p.order_id = po.order_id AND p.status = 'succeeded'
JOIN refunds r ON r.payment_id = p.payment_id AND r.status = 'completed'
""".strip(), "money_cents", False, None, ctx("completed_refunds", filters=["partial_refund_order = true"])),
            _turn("按客户城市拆开。", f"""
WITH {partial_orders_cte()}
SELECT c.city, COALESCE(SUM(r.amount_cents), 0) AS refunded_cents
FROM partial_orders po
JOIN orders o ON o.order_id = po.order_id
JOIN customers c ON c.customer_id = o.customer_id
JOIN payments p ON p.order_id = po.order_id AND p.status = 'succeeded'
JOIN refunds r ON r.payment_id = p.payment_id AND r.status = 'completed'
GROUP BY c.city
""".strip(), "rows", False, None, ctx("completed_refunds", filters=["partial_refund_order = true"], group_by=["city"])),
            _turn("只看上海。", f"""
WITH {partial_orders_cte()}
SELECT COALESCE(SUM(r.amount_cents), 0) AS refunded_cents
FROM partial_orders po
JOIN orders o ON o.order_id = po.order_id
JOIN customers c ON c.customer_id = o.customer_id AND c.city = '上海'
JOIN payments p ON p.order_id = po.order_id AND p.status = 'succeeded'
JOIN refunds r ON r.payment_id = p.payment_id AND r.status = 'completed'
""".strip(), "money_cents", False, None, ctx("completed_refunds", filters=["partial_refund_order = true", "city = 上海"])),
        ]),
        ("M13", [
            _turn("2026年10月净收款额是多少？", net_total(*OCT), "money_cents", False, None, ctx("net_receipts", start=OCT[0], end=OCT[1])),
            _turn("那9月呢？", net_total(*SEP), "money_cents", False, None, ctx("net_receipts", start=SEP[0], end=SEP[1])),
            _turn("10月比9月多多少？", f"""
SELECT ({_net_scalar(*OCT)}) - ({_net_scalar(*SEP)}) AS growth_cents
""".strip(), "money_cents", False, None, ctx("net_receipt_growth", group_by=["month"])),
            _turn("增长率是多少？9月净收款为0时增长率没有定义。", f"""
SELECT CASE WHEN ({_net_scalar(*SEP)}) = 0 THEN NULL
            ELSE (({_net_scalar(*OCT)}) - ({_net_scalar(*SEP)})) * 1.0 / ({_net_scalar(*SEP)})
       END AS growth_rate
""".strip(), "real", False, 1e-9, ctx("net_receipt_growth_rate")),
        ]),
        ("M14", [
            _turn("按订单数量给客户排个名。", """
SELECT cu.customer_id, cu.full_name, COUNT(DISTINCT o.order_id) AS order_count
FROM customers cu JOIN orders o ON o.customer_id = cu.customer_id
GROUP BY cu.customer_id, cu.full_name
ORDER BY order_count DESC, cu.full_name ASC, cu.customer_id ASC
""".strip(), "rows", True, None, ctx("order_count", group_by=["customer"])),
            _turn("只看前五名。", """
SELECT cu.customer_id, cu.full_name, COUNT(DISTINCT o.order_id) AS order_count
FROM customers cu JOIN orders o ON o.customer_id = cu.customer_id
GROUP BY cu.customer_id, cu.full_name
ORDER BY order_count DESC, cu.full_name ASC, cu.customer_id ASC
LIMIT 5
""".strip(), "rows", True, None, ctx("order_count", filters=["top_k = 5"], group_by=["customer"])),
            _turn("这五位客户分别在哪些城市？", """
WITH top5 AS (
    SELECT cu.customer_id
    FROM customers cu JOIN orders o ON o.customer_id = cu.customer_id
    GROUP BY cu.customer_id, cu.full_name
    ORDER BY COUNT(DISTINCT o.order_id) DESC, cu.full_name ASC, cu.customer_id ASC
    LIMIT 5
)
SELECT cu.customer_id, cu.full_name, cu.city
FROM customers cu
WHERE cu.customer_id IN (SELECT customer_id FROM top5)
""".strip(), "rows", False, None, ctx("customer_city", filters=["customer IN top5_order_count"])),
            _turn("把这五位客户的订单按城市合计。", """
WITH top5 AS (
    SELECT cu.customer_id
    FROM customers cu JOIN orders o ON o.customer_id = cu.customer_id
    GROUP BY cu.customer_id, cu.full_name
    ORDER BY COUNT(DISTINCT o.order_id) DESC, cu.full_name ASC, cu.customer_id ASC
    LIMIT 5
)
SELECT cu.city, COUNT(DISTINCT o.order_id) AS order_count
FROM customers cu
JOIN orders o ON o.customer_id = cu.customer_id
WHERE cu.customer_id IN (SELECT customer_id FROM top5)
GROUP BY cu.city
""".strip(), "rows", False, None, ctx("order_count", filters=["customer IN top5_order_count"], group_by=["city"])),
        ]),
        ("M15", [
            _turn("数码类有哪些商品？", """
SELECT p.product_id, p.product_name
FROM products p JOIN categories c ON c.category_id = p.category_id
WHERE c.category_name = '数码'
""".strip(), "rows", False, None, ctx("products", filters=["category = 数码"])),
            _turn("这类商品的历史成交金额一共是多少？", """
SELECT COALESCE(SUM(oi.quantity * oi.unit_price_cents - oi.discount_cents), 0) AS merchandise_cents
FROM order_items oi
JOIN products p ON p.product_id = oi.product_id
JOIN categories c ON c.category_id = p.category_id
WHERE c.category_name = '数码'
""".strip(), "money_cents", False, None, ctx("merchandise", filters=["category = 数码"])),
            _turn("按订单月份汇总。", """
SELECT substr(o.ordered_at, 1, 7) AS ordered_month,
       SUM(oi.quantity * oi.unit_price_cents - oi.discount_cents) AS merchandise_cents
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
JOIN categories c ON c.category_id = p.category_id
WHERE c.category_name = '数码'
GROUP BY substr(o.ordered_at, 1, 7)
""".strip(), "rows", False, None, ctx("merchandise", filters=["category = 数码"], group_by=["month"])),
            _turn("比较9月和10月。", f"""
WITH months(month, start_at, end_at) AS (
    SELECT '2026-09', '{SEP[0]}', '{SEP[1]}'
    UNION ALL
    SELECT '2026-10', '{OCT[0]}', '{OCT[1]}'
)
SELECT m.month,
       COALESCE(SUM(CASE WHEN c.category_name = '数码'
           THEN oi.quantity * oi.unit_price_cents - oi.discount_cents END), 0) AS merchandise_cents
FROM months m
LEFT JOIN orders o ON o.ordered_at >= m.start_at AND o.ordered_at < m.end_at
LEFT JOIN order_items oi ON oi.order_id = o.order_id
LEFT JOIN products p ON p.product_id = oi.product_id
LEFT JOIN categories c ON c.category_id = p.category_id
GROUP BY m.month
""".strip(), "rows", False, None, ctx("merchandise", filters=["category = 数码", "month IN (2026-09, 2026-10)"], group_by=["month"])),
        ]),
        ("M16", [
            _turn("2026年10月成功收款总额是多少？", captured(*OCT), "money_cents", False, None, ctx("captured_receipts", start=OCT[0], end=OCT[1])),
            _turn("只看微信支付。", f"""
SELECT COALESCE(SUM(amount_cents), 0) AS captured_cents
FROM payments
WHERE status = 'succeeded' AND payment_method = 'wechat'
  AND paid_at >= '{OCT[0]}' AND paid_at < '{OCT[1]}'
""".strip(), "money_cents", False, None, ctx("captured_receipts", start=OCT[0], end=OCT[1], filters=["payment_method = wechat"])),
            _turn("按客户城市拆开。", f"""
SELECT c.city, COALESCE(SUM(p.amount_cents), 0) AS captured_cents
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id
LEFT JOIN payments p ON p.order_id = o.order_id
  AND p.status = 'succeeded' AND p.payment_method = 'wechat'
  AND p.paid_at >= '{OCT[0]}' AND p.paid_at < '{OCT[1]}'
GROUP BY c.city
""".strip(), "rows", False, None, ctx("captured_receipts", start=OCT[0], end=OCT[1], filters=["payment_method = wechat"], group_by=["city"])),
            _turn("改看支付宝。", f"""
SELECT c.city, COALESCE(SUM(p.amount_cents), 0) AS captured_cents
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id
LEFT JOIN payments p ON p.order_id = o.order_id
  AND p.status = 'succeeded' AND p.payment_method = 'alipay'
  AND p.paid_at >= '{OCT[0]}' AND p.paid_at < '{OCT[1]}'
GROUP BY c.city
""".strip(), "rows", False, None, ctx("captured_receipts", start=OCT[0], end=OCT[1], filters=["payment_method = alipay"], group_by=["city"])),
        ]),
        ("M17", [
            _turn("一共有多少客户注册？", "SELECT COUNT(*) AS customer_count FROM customers", "count", False, None, ctx("customer_count")),
            _turn("只看活跃客户。", "SELECT COUNT(*) AS customer_count FROM customers WHERE is_active = 1", "count", False, None, ctx("customer_count", filters=["is_active = 1"])),
            _turn("只看上海。", "SELECT COUNT(*) AS customer_count FROM customers WHERE is_active = 1 AND city = '上海'", "count", False, None, ctx("customer_count", filters=["is_active = 1", "city = 上海"])),
            _turn("改成北京。", "SELECT COUNT(*) AS customer_count FROM customers WHERE is_active = 1 AND city = '北京'", "count", False, None, ctx("customer_count", filters=["is_active = 1", "city = 北京"])),
        ]),
        ("M18", [
            _turn("2026年10月已完成退款总额是多少？", refunded(*OCT), "money_cents", False, None, ctx("completed_refunds", start=OCT[0], end=OCT[1])),
            _turn("找出这些退款对应的客户。", f"""
SELECT DISTINCT c.customer_id, c.full_name
FROM refunds r
JOIN payments p ON p.payment_id = r.payment_id
JOIN orders o ON o.order_id = p.order_id
JOIN customers c ON c.customer_id = o.customer_id
WHERE r.status = 'completed'
  AND r.completed_at >= '{OCT[0]}' AND r.completed_at < '{OCT[1]}'
""".strip(), "rows", False, None, ctx("customers", start=OCT[0], end=OCT[1], filters=["has completed refund"])),
            _turn("只看退款金额前五名。并列时按姓名、客户编号升序。", f"""
SELECT c.customer_id, c.full_name, SUM(r.amount_cents) AS refunded_cents
FROM refunds r
JOIN payments p ON p.payment_id = r.payment_id
JOIN orders o ON o.order_id = p.order_id
JOIN customers c ON c.customer_id = o.customer_id
WHERE r.status = 'completed'
  AND r.completed_at >= '{OCT[0]}' AND r.completed_at < '{OCT[1]}'
GROUP BY c.customer_id, c.full_name
ORDER BY refunded_cents DESC, c.full_name ASC, c.customer_id ASC
LIMIT 5
""".strip(), "rows", True, None, ctx("completed_refunds", start=OCT[0], end=OCT[1], filters=["top_k = 5"], group_by=["customer"])),
            _turn("这五位的退款总额是多少？", f"""
WITH ranked AS (
    SELECT c.customer_id, SUM(r.amount_cents) AS refunded_cents
    FROM refunds r
    JOIN payments p ON p.payment_id = r.payment_id
    JOIN orders o ON o.order_id = p.order_id
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE r.status = 'completed'
      AND r.completed_at >= '{OCT[0]}' AND r.completed_at < '{OCT[1]}'
    GROUP BY c.customer_id, c.full_name
    ORDER BY refunded_cents DESC, c.full_name ASC, c.customer_id ASC
    LIMIT 5
)
SELECT COALESCE(SUM(refunded_cents), 0) AS top5_refunded_cents FROM ranked
""".strip(), "money_cents", False, None, ctx("completed_refunds", start=OCT[0], end=OCT[1], filters=["customer IN top5_october_refunds"])),
        ]),
        ("M19", [
            _turn("用 shop_a，2026年10月成功收款总额是多少？", captured(*OCT), "money_cents", False, None, ctx("captured_receipts", source="shop_a", start=OCT[0], end=OCT[1])),
            _turn("明确换成 shop_b，还是查2026年10月成功收款总额。", captured(*OCT), "money_cents", False, None, ctx("captured_receipts", source="shop_b", start=OCT[0], end=OCT[1])),
            _turn("在这个库上按客户城市拆开。", """
WITH captured AS (
    SELECT c.city AS city, SUM(p.amount_cents) AS amount
    FROM payments p
    JOIN orders o ON o.order_id = p.order_id
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE p.status = 'succeeded'
      AND p.paid_at >= '2026-10-01T00:00:00Z' AND p.paid_at < '2026-11-01T00:00:00Z'
    GROUP BY c.city
)
SELECT cities.city, COALESCE(captured.amount, 0) AS captured_cents
FROM (SELECT DISTINCT city FROM customers) AS cities
LEFT JOIN captured ON captured.city = cities.city
""".strip(), "rows", False, None, ctx("captured_receipts", source="shop_b", start=OCT[0], end=OCT[1], group_by=["city"])),
            _turn("把 shop_a 和 shop_b 的10月成功收款放在一起比较相差多少。", None, "money_cents", False, None, {
                "metric": "captured_receipts_difference",
                "date_range": [OCT[0], OCT[1]],
                "filters": [],
                "group_by": ["source_id"],
                "source_id": None,
                "comparison": ["shop_a", "shop_b"],
            }, status="not_implemented", reason="cross_database_comparison"),
        ]),
        ("M20", [
            _turn("2026年10月净收款额按客户城市统计。", net_by_city(*OCT), "rows", False, None, ctx("net_receipts", start=OCT[0], end=OCT[1], group_by=["city"])),
            _turn("只看上海和北京。", net_by_city(*OCT, cities=("上海", "北京")), "rows", False, None, ctx("net_receipts", start=OCT[0], end=OCT[1], filters=["city IN (上海, 北京)"], group_by=["city"])),
            _turn("不用只看这两个城市了，撤销刚才的筛选。", net_by_city(*OCT), "rows", False, None, ctx("net_receipts", start=OCT[0], end=OCT[1], group_by=["city"])),
            _turn("把按城市统计的结果再列一次。", net_by_city(*OCT), "rows", False, None, ctx("net_receipts", start=OCT[0], end=OCT[1], group_by=["city"])),
        ]),
    ]
    if len(sessions) != 20:
        raise RuntimeError(f"Expected 20 sessions, found {len(sessions)}")
    built = []
    for conversation_id, turns in sessions:
        if len(turns) != 4:
            raise RuntimeError(f"{conversation_id} does not have 4 turns")
        source_for_turns = []
        normalized = []
        for index, turn in enumerate(turns, 1):
            source_id = turn["expected_context"].get("source_id") or "shop_a"
            if turn["status"] == "not_implemented":
                source_id = None
            source_for_turns.append(source_id)
            normalized.append({
                "turn_id": f"{conversation_id}-T{index}",
                "source_id": source_id,
                **turn,
            })
        built.append({
            "conversation_id": conversation_id,
            "source_id": "shop_a",
            "turns": normalized,
        })
    return built


JOIN_DUPLICATION = """
WITH captured AS (
    SELECT COALESCE(SUM(p.amount_cents), 0) AS amount
    FROM payments p
    LEFT JOIN refunds r ON r.payment_id = p.payment_id
    WHERE p.status = 'succeeded'
      AND p.paid_at >= '2026-10-01T00:00:00Z'
      AND p.paid_at < '2026-11-01T00:00:00Z'
),
returned AS (
    SELECT COALESCE(SUM(r.amount_cents), 0) AS amount
    FROM payments p
    JOIN refunds r ON r.payment_id = p.payment_id
    WHERE p.status = 'succeeded'
      AND p.paid_at >= '2026-10-01T00:00:00Z'
      AND p.paid_at < '2026-11-01T00:00:00Z'
)
SELECT captured.amount - returned.amount AS net_receipts_cents
FROM captured CROSS JOIN returned
""".strip()

WRONG_DATE = """
SELECT COALESCE(SUM(p.amount_cents), 0) AS captured_cents
FROM payments p
JOIN orders o ON o.order_id = p.order_id
WHERE p.status = 'succeeded'
  AND o.ordered_at >= '2026-10-01T00:00:00Z'
  AND o.ordered_at < '2026-11-01T00:00:00Z'
""".strip()

MISSING_PAYMENT_STATUS = """
SELECT COALESCE(SUM(amount_cents), 0) AS captured_cents
FROM payments
WHERE paid_at >= '2026-10-01T00:00:00Z'
  AND paid_at < '2026-11-01T00:00:00Z'
""".strip()

MISSING_REFUND_STATUS = """
SELECT COALESCE(SUM(amount_cents), 0) AS refunded_cents
FROM refunds
WHERE completed_at >= '2026-10-01T00:00:00Z'
  AND completed_at < '2026-11-01T00:00:00Z'
""".strip()

MISSING_DISTINCT = """
SELECT cu.customer_id, cu.full_name, COUNT(o.order_id) AS order_count
FROM customers cu
JOIN orders o ON o.customer_id = cu.customer_id
JOIN order_items oi ON oi.order_id = o.order_id
GROUP BY cu.customer_id, cu.full_name
""".strip()


def adversarial_probes(cases: list[dict]) -> list[dict]:
    by_id = {row["case_id"]: row for row in cases}
    return [
        {"probe_id": "P-correct-S16", "case_id": "S16", "source_id": "shop_a",
         "predicted_sql": by_id["S16"]["sql"], "expect_sql_correct": True},
        {"probe_id": "P-join-multiplication", "case_id": "S16", "source_id": "shop_a",
         "predicted_sql": " ".join(JOIN_DUPLICATION.split()), "expect_sql_correct": False},
        {"probe_id": "P-wrong-date", "case_id": "S14", "source_id": "shop_a",
         "predicted_sql": " ".join(WRONG_DATE.split()), "expect_sql_correct": False},
        {"probe_id": "P-missing-payment-status", "case_id": "S14", "source_id": "shop_a",
         "predicted_sql": " ".join(MISSING_PAYMENT_STATUS.split()), "expect_sql_correct": False},
        {"probe_id": "P-missing-refund-status", "case_id": "S15", "source_id": "shop_a",
         "predicted_sql": " ".join(MISSING_REFUND_STATUS.split()), "expect_sql_correct": False},
        {"probe_id": "P-missing-distinct", "case_id": "S17", "source_id": "shop_a",
         "predicted_sql": " ".join(MISSING_DISTINCT.split()), "expect_sql_correct": False},
    ]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
    path.write_text(body, encoding="utf-8", newline="\n")


def materialize(root: Path = ROOT) -> None:
    cases = single_cases()
    sessions = multi_sessions()
    write_jsonl(root / "cases" / "single_turn.jsonl", [
        {key: row[key] for key in ("case_id", "source_id", "question", "gold_id", "answer_type", "order_sensitive", "tags")}
        for row in cases
    ])
    write_jsonl(root / "gold" / "single_turn_gold.jsonl", [
        {key: row[key] for key in ("gold_id", "case_id", "source_id", "sql", "answer_type", "order_sensitive", "numeric_tolerance")}
        for row in cases
    ])
    write_jsonl(root / "cases" / "multi_turn.jsonl", [
        {"conversation_id": session["conversation_id"], "source_id": session["source_id"],
         "turns": [{"turn_id": turn["turn_id"], "source_id": turn["source_id"], "question": turn["question"]}
                   for turn in session["turns"]]}
        for session in sessions
    ])
    gold_turns = []
    for session in sessions:
        for turn in session["turns"]:
            gold_turns.append({
                "conversation_id": session["conversation_id"],
                "turn_id": turn["turn_id"],
                "gold_id": turn["turn_id"].replace("-T", "-G"),
                "source_id": turn["source_id"],
                "sql": turn["sql"],
                "answer_type": turn["answer_type"],
                "order_sensitive": turn["order_sensitive"],
                "numeric_tolerance": turn["numeric_tolerance"],
                "status": turn["status"],
                "reason": turn.get("reason"),
                "expected_context": turn["expected_context"],
            })
    write_jsonl(root / "gold" / "multi_turn_gold.jsonl", gold_turns)
    write_jsonl(root / "fixtures" / "predictions.jsonl", adversarial_probes(cases))
    write_jsonl(root / "cases" / "safety.jsonl", [
        {"case_id": "SAFE-DELETE", "expect": "rejected"},
        {"case_id": "SAFE-DROP", "expect": "rejected"},
        {"case_id": "SAFE-MULTI", "expect": "rejected"},
        {"case_id": "SAFE-DB-ID", "expect": "rejected"},
        {"case_id": "SAFE-BYTES", "expect": "unchanged"},
        {"case_id": "SAFE-TIMEOUT", "expect": "timeout"},
        {"case_id": "SAFE-PREVIEW", "expect": "full_result_not_preview"},
        {"case_id": "SAFE-NO-GOLD-INPUT", "expect": "gold_absent"},
        {"case_id": "SAFE-NO-PATH", "expect": "path_absent"},
        {"case_id": "SAFE-TENANT", "expect": "not_implemented", "reason": "multi_tenant_auth"},
    ])
    write_jsonl(root / "cases" / "datasource.jsonl", [
        {"case_id": "DS-COUNT", "metric": "customer_count", "sources": ["shop_a", "shop_b"], "expect": "different"},
        {"case_id": "DS-SAME-TABLE", "table": "customers", "expect": "no_cross_db_mix"},
        {"case_id": "DS-CROSS", "expect": "not_implemented", "reason": "cross_database_comparison"},
    ])


if __name__ == "__main__":
    materialize()
