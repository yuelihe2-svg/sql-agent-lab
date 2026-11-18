PRAGMA foreign_keys = ON;

CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT UNIQUE,
    city TEXT NOT NULL,
    signup_at TEXT NOT NULL,
    is_active INTEGER NOT NULL
        CHECK (is_active IN (0, 1))
);

CREATE TABLE categories (
    category_id INTEGER PRIMARY KEY,
    category_name TEXT NOT NULL UNIQUE
);

CREATE TABLE products (
    product_id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    list_price_cents INTEGER NOT NULL
        CHECK (list_price_cents >= 0),
    is_active INTEGER NOT NULL
        CHECK (is_active IN (0, 1)),
    FOREIGN KEY (category_id)
        REFERENCES categories(category_id)
);

CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    ordered_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending',
            'paid',
            'partially_refunded',
            'refunded',
            'cancelled'
        )
    ),
    shipping_city TEXT,
    FOREIGN KEY (customer_id)
        REFERENCES customers(customer_id)
);

CREATE TABLE order_items (
    item_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL
        CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL
        CHECK (unit_price_cents >= 0),
    discount_cents INTEGER NOT NULL DEFAULT 0
        CHECK (discount_cents >= 0),
    CHECK (
        discount_cents <= quantity * unit_price_cents
    ),
    FOREIGN KEY (order_id)
        REFERENCES orders(order_id),
    FOREIGN KEY (product_id)
        REFERENCES products(product_id)
);

CREATE TABLE payments (
    payment_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    paid_at TEXT NOT NULL,
    amount_cents INTEGER NOT NULL
        CHECK (amount_cents > 0),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'succeeded', 'failed')
    ),
    payment_method TEXT NOT NULL CHECK (
        payment_method IN ('card', 'alipay', 'wechat')
    ),
    FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
);

CREATE TABLE refunds (
    refund_id INTEGER PRIMARY KEY,
    payment_id INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    amount_cents INTEGER NOT NULL
        CHECK (amount_cents > 0),
    status TEXT NOT NULL CHECK (
        status IN ('requested', 'completed', 'rejected')
    ),
    FOREIGN KEY (payment_id)
        REFERENCES payments(payment_id)
);

CREATE TABLE shipments (
    shipment_id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    shipped_at TEXT,
    delivered_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending',
            'shipped',
            'delivered',
            'returned'
        )
    ),
    FOREIGN KEY (order_id)
        REFERENCES orders(order_id)
);

CREATE INDEX idx_customers_city ON customers(city);
CREATE INDEX idx_customers_signup_at ON customers(signup_at);
CREATE INDEX idx_products_category ON products(category_id);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_ordered_at ON orders(ordered_at);
CREATE INDEX idx_orders_status ON orders(status);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);
CREATE INDEX idx_payments_order ON payments(order_id);
CREATE INDEX idx_payments_paid_at ON payments(paid_at);
CREATE INDEX idx_payments_status ON payments(status);
CREATE INDEX idx_refunds_payment ON refunds(payment_id);
CREATE INDEX idx_refunds_completed_at ON refunds(completed_at);
CREATE INDEX idx_refunds_status ON refunds(status);
CREATE INDEX idx_shipments_order ON shipments(order_id);
