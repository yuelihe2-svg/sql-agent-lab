"""Reproducible ShopBench v1 SQLite databases. Synthetic rows only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import hashlib
import json
import random
import sqlite3


DATASET_VERSION = "shopbench_v1"
GENERATOR_VERSION = "shopbench-seed-1"
RULES_VERSION = "shopbench-rules-1"
EVAL_NOW = "2026-11-18T00:00:00Z"
ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "schema.sql"
RULES_PATH = ROOT / "business_rules.md"
TABLES = (
    "customers",
    "categories",
    "products",
    "orders",
    "order_items",
    "payments",
    "refunds",
    "shipments",
)
CITIES = ("上海", "北京", "广州", "深圳", "杭州", "成都", "武汉", "南京")
CATEGORIES = ("数码", "家居", "服饰", "食品", "美妆", "运动", "图书", "母婴")
SURNAMES = ("陈", "林", "黄", "张", "李", "王", "赵", "周", "吴", "徐")
GIVEN = ("伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "军", "洋")
METHODS = ("card", "alipay", "wechat")
RANGE_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 11, 17, 23, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    seed: int
    customers: int
    products: int
    orders: int
    order_items: int
    idle_customers: int
    unsold_products: int
    null_emails: int
    null_shipping: int
    boundary: bool = False


SPECS = (
    SourceSpec("shop_a", 20261118, 120, 50, 600, 1400, 18, 6, 8, 40),
    SourceSpec("shop_b", 20261119, 96, 42, 480, 1100, 10, 4, 4, 15),
    SourceSpec("shop_edge", 20261120, 20, 15, 35, 70, 6, 2, 3, 4, True),
)


def schema_text() -> str:
    return SCHEMA_PATH.read_text(encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def random_stamp(rng: random.Random) -> str:
    hours = int((RANGE_END - RANGE_START).total_seconds() // 3600)
    return stamp(RANGE_START + timedelta(hours=rng.randrange(hours + 1)))


def quotas(order_count: int) -> dict[str, int]:
    cancelled = max(2, order_count // 15)
    unpaid = max(2, order_count // 20)
    failed_only = max(2, order_count // 25)
    refunded = max(2, order_count // 10)
    partial = max(2, order_count // 8)
    paid = order_count - (cancelled + unpaid + failed_only + refunded + partial)
    if paid < 5:
        raise RuntimeError(f"Not enough paid orders to plant required cases: {order_count}")
    return {
        "cancelled": cancelled,
        "unpaid": unpaid,
        "failed_only": failed_only,
        "refunded": refunded,
        "partially_refunded": partial,
        "paid": paid,
    }


def status_plan(order_count: int) -> list[str]:
    counts = quotas(order_count)
    plan = (
        ["cancelled"] * counts["cancelled"]
        + ["unpaid"] * counts["unpaid"]
        + ["failed_only"] * counts["failed_only"]
        + ["refunded"] * counts["refunded"]
        + ["partially_refunded"] * counts["partially_refunded"]
        + ["paid"] * counts["paid"]
    )
    if len(plan) != order_count:
        raise RuntimeError("Order status plan does not match the requested order count")
    return plan


def public_status(kind: str) -> str:
    if kind in ("unpaid", "failed_only"):
        return "pending"
    return kind


def generate_database(spec: SourceSpec, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    rng = random.Random(spec.seed)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA page_size=4096")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(schema_text())
        _populate(conn, spec, rng)
        _ensure_month_nets(conn)
        violations = validate_connection(conn)
        if violations:
            raise RuntimeError("Business validation failed: " + "; ".join(violations))
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()
    for suffix in ("-wal", "-shm", "-journal"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            raise RuntimeError(f"Generator left an environment file: {extra.name}")
    return database_record(spec, path)


def _populate(conn: sqlite3.Connection, spec: SourceSpec, rng: random.Random) -> None:
    for index, name in enumerate(CATEGORIES, 1):
        conn.execute("INSERT INTO categories(category_id, category_name) VALUES (?, ?)", (index, name))
    products = []
    for product_id in range(1, spec.products + 1):
        list_price = 800 + ((product_id * 137 + spec.seed) % 9000)
        products.append({
            "product_id": product_id,
            "category_id": ((product_id - 1) % len(CATEGORIES)) + 1,
            "product_name": f"{CATEGORIES[(product_id - 1) % len(CATEGORIES)]}商品{product_id:03d}",
            "list_price_cents": list_price,
            "is_active": 0 if product_id % 17 == 0 else 1,
            "unsold": product_id > spec.products - spec.unsold_products,
        })
        conn.execute(
            "INSERT INTO products(product_id, category_id, product_name, list_price_cents, is_active) "
            "VALUES (?, ?, ?, ?, ?)",
            (product_id, products[-1]["category_id"], products[-1]["product_name"], list_price,
             products[-1]["is_active"]),
        )
    customers = []
    for customer_id in range(1, spec.customers + 1):
        email = None if customer_id <= spec.null_emails else f"c{customer_id:03d}@shop.example"
        signup = random_stamp(rng)
        if customer_id == 3:
            signup = "2026-10-04T08:00:00Z"
        elif customer_id == 4:
            signup = "2026-09-04T08:00:00Z"
        if spec.boundary and customer_id == 1:
            signup = "2026-10-01T00:00:00Z"
        elif spec.boundary and customer_id == 2:
            signup = "2026-11-01T00:00:00Z"
        customers.append({
            "customer_id": customer_id,
            "full_name": f"{SURNAMES[customer_id % 10]}{GIVEN[(customer_id // 10) % 10]}{customer_id:03d}",
            "email": email,
            "city": CITIES[(customer_id - 1) % len(CITIES)],
            "signup_at": signup,
            "is_active": 0 if customer_id % 7 == 0 else 1,
            "idle": customer_id > spec.customers - spec.idle_customers,
        })
        conn.execute(
            "INSERT INTO customers(customer_id, full_name, email, city, signup_at, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (customer_id, customers[-1]["full_name"], email, customers[-1]["city"], signup,
             customers[-1]["is_active"]),
        )
    buyers = [row for row in customers if not row["idle"]]
    sellable = [row for row in products if not row["unsold"]]
    weights = [1 / (1 + index) for index in range(len(buyers))]
    plan = status_plan(spec.orders)
    paid_index = 0
    partial_index = 0
    orders = []
    for order_id, kind in enumerate(plan, 1):
        customer = rng.choices(buyers, weights=weights, k=1)[0]
        ordered_at = random_stamp(rng)
        flags = {
            "two_payments": False,
            "two_refunds": False,
            "oct_nov": False,
            "sep_order_oct_pay": False,
            "oct_order_nov_pay": False,
            "seed_payment": False,
            "failed_october": False,
            "boundary_oct_start": False,
            "boundary_nov_start": False,
        }
        if kind == "paid":
            if paid_index == 0:
                flags["seed_payment"] = True
                ordered_at = "2026-10-02T08:00:00Z"
            elif paid_index in (1, 2):
                flags["two_payments"] = True
                ordered_at = "2026-10-05T08:00:00Z"
            elif paid_index == 3:
                flags["sep_order_oct_pay"] = True
                ordered_at = "2026-09-15T10:00:00Z"
            elif paid_index == 4:
                flags["oct_order_nov_pay"] = True
                ordered_at = "2026-10-12T10:00:00Z"
            paid_index += 1
        elif kind == "partially_refunded":
            if partial_index == 0:
                flags["two_refunds"] = True
                ordered_at = "2026-10-06T09:00:00Z"
            elif partial_index == 1:
                flags["oct_nov"] = True
                ordered_at = "2026-10-18T09:00:00Z"
            partial_index += 1
        elif kind == "failed_only" and not any(row.get("failed_october") for row in orders):
            flags["failed_october"] = True
            ordered_at = "2026-10-08T09:00:00Z"
        if spec.boundary and kind == "paid" and paid_index == 6:
            flags["boundary_oct_start"] = True
            ordered_at = "2026-10-01T00:00:00Z"
        elif spec.boundary and kind == "paid" and paid_index == 7:
            flags["boundary_nov_start"] = True
            ordered_at = "2026-10-30T12:00:00Z"
        if order_id <= spec.null_shipping:
            shipping = None
        elif order_id == spec.null_shipping + 1:
            shipping = CITIES[(CITIES.index(customer["city"]) + 1) % len(CITIES)]
        elif rng.random() < 0.25:
            shipping = CITIES[(CITIES.index(customer["city"]) + 1 + rng.randrange(7)) % len(CITIES)]
        else:
            shipping = customer["city"]
        orders.append({
            "order_id": order_id,
            "customer_id": customer["customer_id"],
            "ordered_at": ordered_at,
            "kind": kind,
            "status": public_status(kind),
            "shipping_city": shipping,
            **flags,
        })
        conn.execute(
            "INSERT INTO orders(order_id, customer_id, ordered_at, status, shipping_city) "
            "VALUES (?, ?, ?, ?, ?)",
            (order_id, customer["customer_id"], ordered_at, public_status(kind), shipping),
        )
    item_id = 1
    payment_id = 1
    refund_id = 1
    planted_price_gap = False
    planted_rejected = False
    planted_requested = False
    planted_pending = False
    for order in orders:
        item_id = _insert_item(conn, rng, order, rng.choice(sellable), item_id, force_gap=not planted_price_gap)
        planted_price_gap = True
        payment_id, refund_id, planted_rejected, planted_requested, planted_pending = _insert_money(
            conn, rng, spec, order, payment_id, refund_id, planted_rejected, planted_requested, planted_pending,
        )
    extra_items = spec.order_items - spec.orders
    for _ in range(extra_items):
        order = rng.choice(orders)
        item_id = _insert_item(conn, rng, order, rng.choice(sellable), item_id, force_gap=False)
    for order in orders:
        shipment_id = order["order_id"]
        if order["status"] == "cancelled":
            status, shipped, delivered = "returned", order["ordered_at"], None
        elif order["status"] == "pending":
            status, shipped, delivered = "pending", None, None
        elif rng.random() < 0.5:
            status, shipped, delivered = "shipped", order["ordered_at"], None
        else:
            status, shipped, delivered = "delivered", order["ordered_at"], order["ordered_at"]
        conn.execute(
            "INSERT INTO shipments(shipment_id, order_id, shipped_at, delivered_at, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (shipment_id, order["order_id"], shipped, delivered, status),
        )


def _insert_item(conn, rng, order, product, item_id: int, *, force_gap: bool) -> int:
    quantity = 1 + rng.randrange(3)
    if force_gap or rng.random() < 0.35:
        unit = max(50, product["list_price_cents"] + rng.choice((-300, -200, 250, 400)))
        if unit == product["list_price_cents"]:
            unit += 100
    else:
        unit = product["list_price_cents"]
    discount = rng.randrange(unit // 10 + 1) if rng.random() < 0.2 else 0
    conn.execute(
        "INSERT INTO order_items(item_id, order_id, product_id, quantity, unit_price_cents, discount_cents) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, order["order_id"], product["product_id"], quantity, unit, discount),
    )
    return item_id + 1


def _insert_payment(conn, payment_id, order_id, paid_at, amount, status, method) -> int:
    conn.execute(
        "INSERT INTO payments(payment_id, order_id, paid_at, amount_cents, status, payment_method) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (payment_id, order_id, paid_at, amount, status, method),
    )
    return payment_id + 1


def _insert_refund(conn, refund_id, payment_id, requested_at, completed_at, amount, status) -> int:
    conn.execute(
        "INSERT INTO refunds(refund_id, payment_id, requested_at, completed_at, amount_cents, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (refund_id, payment_id, requested_at, completed_at, amount, status),
    )
    return refund_id + 1


def _insert_money(conn, rng, spec, order, payment_id, refund_id, planted_rejected, planted_requested,
                  planted_pending):
    kind = order["kind"]
    method = METHODS[order["order_id"] % 3]

    def pay(paid_at, amount, status):
        nonlocal payment_id
        current = payment_id
        payment_id = _insert_payment(conn, payment_id, order["order_id"], paid_at, amount, status, method)
        return current

    def refund(target, requested_at, completed_at, amount, status):
        nonlocal refund_id
        refund_id = _insert_refund(conn, refund_id, target, requested_at, completed_at, amount, status)

    if kind == "cancelled":
        if rng.random() < 0.5:
            pay(order["ordered_at"], 500 + rng.randrange(2000), "failed")
        return payment_id, refund_id, planted_rejected, planted_requested, planted_pending
    if kind == "unpaid":
        return payment_id, refund_id, planted_rejected, planted_requested, planted_pending
    if kind == "failed_only":
        paid_at = "2026-10-08T12:00:00Z" if order["failed_october"] else random_stamp(rng)
        pay(paid_at, 700 + rng.randrange(3000), "failed")
        return payment_id, refund_id, planted_rejected, planted_requested, planted_pending

    if order["seed_payment"]:
        first = pay("2026-10-02T09:00:00Z", 1000 + (spec.seed % 997), "succeeded")
        payments = [(first, 1000 + (spec.seed % 997), "2026-10-02T09:00:00Z")]
    elif order["two_payments"]:
        payments = [
            (pay("2026-10-05T09:00:00Z", 4000, "succeeded"), 4000, "2026-10-05T09:00:00Z"),
            (pay("2026-10-05T15:00:00Z", 6000, "succeeded"), 6000, "2026-10-05T15:00:00Z"),
        ]
    elif order["sep_order_oct_pay"]:
        current = pay("2026-10-16T10:00:00Z", 18000, "succeeded")
        payments = [(current, 18000, "2026-10-16T10:00:00Z")]
    elif order["oct_order_nov_pay"]:
        current = pay("2026-11-02T10:00:00Z", 16000, "succeeded")
        payments = [(current, 16000, "2026-11-02T10:00:00Z")]
    elif order["oct_nov"]:
        current = pay("2026-10-20T10:00:00Z", 20000, "succeeded")
        payments = [(current, 20000, "2026-10-20T10:00:00Z")]
    elif order["two_refunds"]:
        current = pay("2026-10-06T10:00:00Z", 12000, "succeeded")
        payments = [(current, 12000, "2026-10-06T10:00:00Z")]
    elif order["boundary_oct_start"]:
        current = pay("2026-10-01T00:00:00Z", 2222, "succeeded")
        payments = [(current, 2222, "2026-10-01T00:00:00Z")]
    elif order["boundary_nov_start"]:
        current = pay("2026-11-01T00:00:00Z", 3333, "succeeded")
        payments = [(current, 3333, "2026-11-01T00:00:00Z")]
    else:
        paid_at = random_stamp(rng)
        amount = 500 + rng.randrange(20000)
        current = pay(paid_at, amount, "succeeded")
        payments = [(current, amount, paid_at)]

    if kind == "paid" and not planted_pending:
        pay("2026-10-11T11:00:00Z", 900, "pending")
        planted_pending = True
    elif kind == "paid" and rng.random() < 0.04:
        pay(random_stamp(rng), 600, "pending")

    if kind == "partially_refunded" and order["two_refunds"]:
        target, _, paid_at = payments[0]
        refund(target, paid_at, "2026-10-21T10:00:00Z", 4000, "completed")
        refund(target, paid_at, "2026-10-22T10:00:00Z", 3000, "completed")
    elif kind == "partially_refunded" and order["oct_nov"]:
        target, amount, paid_at = payments[0]
        refund(target, paid_at, "2026-11-05T10:00:00Z", 5000, "completed")
    elif kind == "partially_refunded":
        target, amount, paid_at = payments[0]
        refund_amount = max(1, amount // 3)
        if refund_amount >= amount:
            refund_amount = amount - 1
        refund(target, paid_at, paid_at, refund_amount, "completed")
    elif kind == "refunded":
        for target, amount, paid_at in payments:
            refund(target, paid_at, paid_at, amount, "completed")

    if not planted_rejected and payments:
        target, _, _ = payments[0]
        refund(target, "2026-10-15T08:00:00Z", "2026-10-15T09:00:00Z", 100, "rejected")
        planted_rejected = True
    if not planted_requested and payments:
        target, _, _ = payments[0]
        refund(target, "2026-10-15T12:00:00Z", None, 100, "requested")
        planted_requested = True
    return payment_id, refund_id, planted_rejected, planted_requested, planted_pending


def _month_net(conn, start: str, end: str) -> int:
    captured = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM payments "
        "WHERE status = 'succeeded' AND paid_at >= ? AND paid_at < ?",
        (start, end),
    ).fetchone()[0]
    returned = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM refunds "
        "WHERE status = 'completed' AND completed_at >= ? AND completed_at < ?",
        (start, end),
    ).fetchone()[0]
    return int(captured) - int(returned)


def _ensure_month_nets(conn) -> None:
    windows = (
        ("2026-09-01T00:00:00Z", "2026-10-01T00:00:00Z", "2026-09-10T10:00:00Z"),
        ("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z", "2026-10-19T10:00:00Z"),
    )
    for start, end, when in windows:
        if _month_net(conn, start, end) != 0:
            continue
        order_id = conn.execute("SELECT order_id FROM orders WHERE status = 'paid' ORDER BY order_id LIMIT 1").fetchone()
        if order_id is None:
            raise RuntimeError("No paid order available to keep monthly net receipts defined")
        payment_id = conn.execute("SELECT COALESCE(MAX(payment_id), 0) + 1 FROM payments").fetchone()[0]
        conn.execute(
            "INSERT INTO payments(payment_id, order_id, paid_at, amount_cents, status, payment_method) "
            "VALUES (?, ?, ?, ?, 'succeeded', 'card')",
            (payment_id, order_id[0], when, 100),
        )


def validate_connection(conn: sqlite3.Connection) -> list[str]:
    problems = []
    conn.execute("PRAGMA foreign_keys=ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        problems.append("foreign keys are off")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        problems.append("foreign key check failed")
    checks = {
        "order without items": """
            SELECT COUNT(*) FROM orders o
            WHERE NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id)
        """,
        "cancelled order with succeeded payment": """
            SELECT COUNT(*) FROM orders o
            WHERE o.status = 'cancelled' AND EXISTS (
                SELECT 1 FROM payments p
                WHERE p.order_id = o.order_id AND p.status = 'succeeded')
        """,
        "succeeded payment on cancelled order": """
            SELECT COUNT(*) FROM payments p
            JOIN orders o ON o.order_id = p.order_id
            WHERE p.status = 'succeeded' AND o.status = 'cancelled'
        """,
        "completed refund on non-succeeded payment": """
            SELECT COUNT(*) FROM refunds r
            JOIN payments p ON p.payment_id = r.payment_id
            WHERE r.status = 'completed' AND p.status != 'succeeded'
        """,
        "completed refund missing timestamp": """
            SELECT COUNT(*) FROM refunds
            WHERE status = 'completed' AND completed_at IS NULL
        """,
        "negative line amount": """
            SELECT COUNT(*) FROM order_items
            WHERE quantity * unit_price_cents - discount_cents < 0
        """,
        "refund exceeds payment": """
            SELECT COUNT(*) FROM (
                SELECT p.payment_id
                FROM payments p
                JOIN refunds r ON r.payment_id = p.payment_id AND r.status = 'completed'
                GROUP BY p.payment_id, p.amount_cents
                HAVING SUM(r.amount_cents) > p.amount_cents
            )
        """,
        "december activity": """
            SELECT
                (SELECT COUNT(*) FROM orders WHERE ordered_at >= '2026-12-01T00:00:00Z')
                + (SELECT COUNT(*) FROM payments WHERE paid_at >= '2026-12-01T00:00:00Z')
                + (SELECT COUNT(*) FROM refunds WHERE completed_at >= '2026-12-01T00:00:00Z')
        """,
    }
    for name, sql in checks.items():
        if conn.execute(sql).fetchone()[0] != 0:
            problems.append(name)
    required = {
        "multiple items on one order": "SELECT COUNT(*) FROM (SELECT order_id FROM order_items GROUP BY order_id HAVING COUNT(*) > 1)",
        "two succeeded payments": """
            SELECT COUNT(*) FROM (
                SELECT order_id FROM payments WHERE status = 'succeeded'
                GROUP BY order_id HAVING COUNT(*) >= 2)
        """,
        "two completed refunds": """
            SELECT COUNT(*) FROM (
                SELECT payment_id FROM refunds WHERE status = 'completed'
                GROUP BY payment_id HAVING COUNT(*) >= 2)
        """,
        "failed payment only": """
            SELECT COUNT(*) FROM orders o
            WHERE EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'failed')
              AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id AND p.status = 'succeeded')
        """,
        "order without payment": """
            SELECT COUNT(*) FROM orders o
            WHERE NOT EXISTS (SELECT 1 FROM payments p WHERE p.order_id = o.order_id)
        """,
        "october payment november refund": """
            SELECT COUNT(*) FROM refunds r
            JOIN payments p ON p.payment_id = r.payment_id
            WHERE r.status = 'completed' AND p.status = 'succeeded'
              AND p.paid_at >= '2026-10-01T00:00:00Z' AND p.paid_at < '2026-11-01T00:00:00Z'
              AND r.completed_at >= '2026-11-01T00:00:00Z' AND r.completed_at < '2026-12-01T00:00:00Z'
        """,
        "partial refund order": """
            SELECT COUNT(*) FROM (
                SELECT p.order_id
                FROM payments p
                JOIN refunds r ON r.payment_id = p.payment_id AND r.status = 'completed'
                WHERE p.status = 'succeeded'
                GROUP BY p.order_id
                HAVING SUM(r.amount_cents) > 0
                   AND SUM(r.amount_cents) < (
                       SELECT SUM(amount_cents) FROM payments
                       WHERE order_id = p.order_id AND status = 'succeeded')
            )
        """,
        "full refund order": """
            SELECT COUNT(*) FROM (
                SELECT p.order_id
                FROM payments p
                JOIN refunds r ON r.payment_id = p.payment_id AND r.status = 'completed'
                WHERE p.status = 'succeeded'
                GROUP BY p.order_id
                HAVING SUM(r.amount_cents) = (
                    SELECT SUM(amount_cents) FROM payments
                    WHERE order_id = p.order_id AND status = 'succeeded')
            )
        """,
        "shipping city differs": """
            SELECT COUNT(*) FROM orders o
            JOIN customers c ON c.customer_id = o.customer_id
            WHERE o.shipping_city IS NOT NULL AND o.shipping_city != c.city
        """,
        "historical price differs": """
            SELECT COUNT(*) FROM order_items oi
            JOIN products p ON p.product_id = oi.product_id
            WHERE oi.unit_price_cents != p.list_price_cents
        """,
        "null email": "SELECT COUNT(*) FROM customers WHERE email IS NULL",
        "null shipping": "SELECT COUNT(*) FROM orders WHERE shipping_city IS NULL",
        "customer without orders": """
            SELECT COUNT(*) FROM customers c
            WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id)
        """,
        "unsold product": """
            SELECT COUNT(*) FROM products p
            WHERE NOT EXISTS (SELECT 1 FROM order_items oi WHERE oi.product_id = p.product_id)
        """,
        "rejected refund": "SELECT COUNT(*) FROM refunds WHERE status = 'rejected'",
        "requested refund": "SELECT COUNT(*) FROM refunds WHERE status = 'requested'",
    }
    for name, sql in required.items():
        if conn.execute(sql).fetchone()[0] < 1:
            problems.append("missing " + name)
    return problems


def content_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    conn = sqlite3.connect(path)
    try:
        master = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        digest.update(json.dumps(master, ensure_ascii=False).encode("utf-8"))
        for table in TABLES:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            digest.update(table.encode("utf-8"))
            digest.update(json.dumps(rows, ensure_ascii=False).encode("utf-8"))
    finally:
        conn.close()
    return digest.hexdigest()


def row_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}
    finally:
        conn.close()


def database_record(spec: SourceSpec, path: Path) -> dict:
    return {
        "source_id": spec.source_id,
        "filename": path.name,
        "seed": spec.seed,
        "dataset_version": DATASET_VERSION,
        "generator_version": GENERATOR_VERSION,
        "business_rules_version": RULES_VERSION,
        "schema_sha256": sha256_bytes(schema_text().encode("utf-8")),
        "business_rules_sha256": sha256_bytes(RULES_PATH.read_bytes()),
        "file_sha256": sha256_file(path),
        "content_sha256": content_sha256(path),
        "row_counts": row_counts(path),
        "sqlite_version": sqlite3.sqlite_version,
        "eval_now": EVAL_NOW,
    }


def generate_all(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    databases = [generate_database(spec, output_dir / f"{spec.source_id}.sqlite") for spec in SPECS]
    manifest = {
        "dataset_version": DATASET_VERSION,
        "generator_version": GENERATOR_VERSION,
        "business_rules_version": RULES_VERSION,
        "scorer": "shopbench_strict_v1",
        "eval_now": EVAL_NOW,
        "sqlite_version": sqlite3.sqlite_version,
        "schema_sha256": sha256_bytes(schema_text().encode("utf-8")),
        "databases": databases,
    }
    (output_dir / "manifest.generated.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate ShopBench v1 SQLite databases")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    manifest = generate_all(args.output_dir)
    print(json.dumps({
        "output_dir": str(args.output_dir),
        "sources": [
            {"source_id": item["source_id"], "seed": item["seed"], "file_sha256": item["file_sha256"],
             "content_sha256": item["content_sha256"], "row_counts": item["row_counts"]}
            for item in manifest["databases"]
        ],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
