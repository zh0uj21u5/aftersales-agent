PRAGMA foreign_keys = ON;

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    default_address TEXT NOT NULL CHECK (json_valid(default_address))
) STRICT;

CREATE TABLE payment_methods (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    type TEXT NOT NULL CHECK (type IN ('alipay', 'wechat', 'card')),
    label TEXT NOT NULL,
    UNIQUE (id, user_id)
) STRICT;

CREATE TABLE spus (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('digital','apparel','home','beauty','fresh','custom')),
    description TEXT NOT NULL
) STRICT;

CREATE TABLE skus (
    id TEXT PRIMARY KEY,
    spu_id TEXT NOT NULL REFERENCES spus(id),
    attrs TEXT NOT NULL CHECK (json_valid(attrs)),
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    stock INTEGER NOT NULL CHECK (stock >= 0)
) STRICT;

CREATE TABLE price_history (
    sku_id TEXT NOT NULL REFERENCES skus(id),
    price_cents INTEGER NOT NULL CHECK (price_cents > 0),
    effective_at TEXT NOT NULL,
    PRIMARY KEY (sku_id, effective_at)
) STRICT;

CREATE TABLE coupons (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    threshold_cents INTEGER NOT NULL CHECK (threshold_cents >= 0),
    discount_cents INTEGER NOT NULL CHECK (discount_cents > 0),
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    CHECK (valid_from <= valid_to)
) STRICT;

CREATE TABLE user_coupons (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    coupon_id TEXT NOT NULL REFERENCES coupons(id),
    status TEXT NOT NULL CHECK (status IN ('unused','used','expired')),
    used_order_id TEXT REFERENCES orders(id) DEFERRABLE INITIALLY DEFERRED,
    CHECK ((status = 'used' AND used_order_id IS NOT NULL)
        OR (status != 'used' AND used_order_id IS NULL))
) STRICT;

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL CHECK (status IN ('pending_payment','pending_shipment','shipped',
        'delivered','completed','aftersale','cancelled')),
    address TEXT NOT NULL CHECK (json_valid(address)),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    paid_at TEXT,
    shipped_at TEXT,
    delivered_at TEXT,
    items_total_cents INTEGER NOT NULL CHECK (items_total_cents > 0),
    discount_cents INTEGER NOT NULL CHECK (discount_cents >= 0 AND discount_cents <= items_total_cents),
    paid_cents INTEGER NOT NULL CHECK (paid_cents >= 0),
    payment_method_id TEXT NOT NULL,
    user_coupon_id TEXT REFERENCES user_coupons(id),
    price_protected INTEGER NOT NULL DEFAULT 0 CHECK (price_protected IN (0,1)),
    FOREIGN KEY (payment_method_id, user_id) REFERENCES payment_methods(id, user_id),
    UNIQUE (id, payment_method_id),
    CHECK ((paid_at IS NULL AND paid_cents = 0)
        OR (paid_at IS NOT NULL AND paid_cents = items_total_cents - discount_cents)),
    CHECK (paid_at IS NULL OR paid_at >= created_at),
    CHECK (shipped_at IS NULL OR (paid_at IS NOT NULL AND shipped_at >= paid_at)),
    CHECK (delivered_at IS NULL OR (shipped_at IS NOT NULL AND delivered_at >= shipped_at))
) STRICT;

CREATE TABLE order_items (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    sku_id TEXT NOT NULL REFERENCES skus(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
    paid_share_cents INTEGER NOT NULL CHECK (paid_share_cents >= 0 AND paid_share_cents <= unit_price_cents * qty),
    status TEXT NOT NULL CHECK (status IN ('normal','returned','exchanged')),
    returned_qty INTEGER NOT NULL DEFAULT 0 CHECK (returned_qty >= 0 AND returned_qty <= qty),
    refunded_cents INTEGER NOT NULL DEFAULT 0 CHECK (refunded_cents >= 0 AND refunded_cents <= paid_share_cents),
    UNIQUE (id, order_id)
) STRICT;

CREATE TABLE shipments (
    order_id TEXT PRIMARY KEY REFERENCES orders(id),
    carrier TEXT NOT NULL,
    tracking_no TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('shipped','delivered'))
) STRICT;

CREATE TABLE aftersales (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    order_item_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('return','exchange','price_protection')),
    status TEXT NOT NULL CHECK (status IN ('completed','awaiting_payment')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    new_sku_id TEXT REFERENCES skus(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_item_id, order_id) REFERENCES order_items(id, order_id),
    UNIQUE (id, order_id),
    CHECK ((type = 'exchange' AND new_sku_id IS NOT NULL)
        OR (type != 'exchange' AND new_sku_id IS NULL)),
    CHECK (status != 'awaiting_payment' OR type = 'exchange')
) STRICT;

CREATE TABLE refunds (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    aftersale_id TEXT,
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    payment_method_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'success'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (order_id, payment_method_id) REFERENCES orders(id, payment_method_id),
    FOREIGN KEY (aftersale_id, order_id) REFERENCES aftersales(id, order_id)
) STRICT;

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    order_id TEXT NOT NULL REFERENCES orders(id),
    action TEXT NOT NULL CHECK (action IN ('apply_price_protection','request_return','request_exchange')),
    args TEXT NOT NULL CHECK (json_valid(args)),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'pending'),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE handoffs (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
