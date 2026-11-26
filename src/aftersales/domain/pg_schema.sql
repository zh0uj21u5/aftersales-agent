-- PostgreSQL 16 / migration 1. Keep the business projection compatible with SQLite.
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    default_address JSONB NOT NULL
);

CREATE TABLE payment_methods (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    type TEXT NOT NULL CHECK (type IN ('alipay', 'wechat', 'card')),
    label TEXT NOT NULL,
    UNIQUE (id, user_id)
);

CREATE TABLE spus (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('digital','apparel','home','beauty','fresh','custom')),
    description TEXT NOT NULL
);

CREATE TABLE skus (
    id TEXT PRIMARY KEY,
    spu_id TEXT NOT NULL REFERENCES spus(id),
    attrs JSONB NOT NULL,
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    stock INTEGER NOT NULL CHECK (stock >= 0)
);

CREATE TABLE price_history (
    sku_id TEXT NOT NULL REFERENCES skus(id),
    price_cents BIGINT NOT NULL CHECK (price_cents > 0),
    effective_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (sku_id, effective_at)
);

CREATE TABLE coupons (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    threshold_cents BIGINT NOT NULL CHECK (threshold_cents >= 0),
    discount_cents BIGINT NOT NULL CHECK (discount_cents > 0),
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ NOT NULL,
    CHECK (valid_from <= valid_to)
);

CREATE TABLE user_coupons (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    coupon_id TEXT NOT NULL REFERENCES coupons(id),
    status TEXT NOT NULL CHECK (status IN ('unused','used','expired')),
    used_order_id TEXT,
    UNIQUE (id, user_id),
    CHECK ((status = 'used' AND used_order_id IS NOT NULL)
        OR (status != 'used' AND used_order_id IS NULL))
);

CREATE TABLE orders (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL CHECK (status IN ('pending_payment','pending_shipment','shipped',
        'delivered','completed','aftersale','cancelled')),
    address JSONB NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    paid_at TIMESTAMPTZ,
    shipped_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    items_total_cents BIGINT NOT NULL CHECK (items_total_cents > 0),
    discount_cents BIGINT NOT NULL CHECK (discount_cents >= 0 AND discount_cents <= items_total_cents),
    paid_cents BIGINT NOT NULL CHECK (paid_cents >= 0),
    payment_method_id TEXT NOT NULL,
    user_coupon_id TEXT,
    price_protected INTEGER NOT NULL DEFAULT 0 CHECK (price_protected IN (0,1)),
    FOREIGN KEY (payment_method_id, user_id) REFERENCES payment_methods(id, user_id),
    FOREIGN KEY (user_coupon_id, user_id) REFERENCES user_coupons(id, user_id),
    UNIQUE (id, payment_method_id),
    UNIQUE (id, user_id),
    CHECK ((paid_at IS NULL AND paid_cents = 0)
        OR (paid_at IS NOT NULL AND paid_cents = items_total_cents - discount_cents)),
    CHECK (paid_at IS NULL OR paid_at >= created_at),
    CHECK (shipped_at IS NULL OR (paid_at IS NOT NULL AND shipped_at >= paid_at)),
    CHECK (delivered_at IS NULL OR (shipped_at IS NOT NULL AND delivered_at >= shipped_at))
);

ALTER TABLE user_coupons ADD CONSTRAINT user_coupon_used_order
    FOREIGN KEY (used_order_id, user_id) REFERENCES orders(id, user_id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE order_items (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    sku_id TEXT NOT NULL REFERENCES skus(id),
    qty INTEGER NOT NULL CHECK (qty > 0),
    unit_price_cents BIGINT NOT NULL CHECK (unit_price_cents > 0),
    paid_share_cents BIGINT NOT NULL CHECK (paid_share_cents >= 0 AND paid_share_cents <= unit_price_cents * qty),
    status TEXT NOT NULL CHECK (status IN ('normal','returned','exchanged')),
    returned_qty INTEGER NOT NULL DEFAULT 0 CHECK (returned_qty >= 0 AND returned_qty <= qty),
    refunded_cents BIGINT NOT NULL DEFAULT 0 CHECK (refunded_cents >= 0 AND refunded_cents <= paid_share_cents),
    UNIQUE (id, order_id)
);

CREATE TABLE shipments (
    order_id TEXT PRIMARY KEY REFERENCES orders(id),
    carrier TEXT NOT NULL,
    tracking_no TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('shipped','delivered'))
);

CREATE TABLE aftersales (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    order_item_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('return','exchange','price_protection')),
    status TEXT NOT NULL CHECK (status IN ('completed','awaiting_payment')),
    qty INTEGER NOT NULL CHECK (qty > 0),
    amount_cents BIGINT NOT NULL CHECK (amount_cents >= 0),
    new_sku_id TEXT REFERENCES skus(id),
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (order_item_id, order_id) REFERENCES order_items(id, order_id),
    UNIQUE (id, order_id),
    CHECK ((type = 'exchange' AND new_sku_id IS NOT NULL)
        OR (type != 'exchange' AND new_sku_id IS NULL)),
    CHECK (status != 'awaiting_payment' OR type = 'exchange')
);

CREATE TABLE refunds (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    aftersale_id TEXT,
    amount_cents BIGINT NOT NULL CHECK (amount_cents >= 0),
    payment_method_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'success'),
    created_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (order_id, payment_method_id) REFERENCES orders(id, payment_method_id),
    FOREIGN KEY (aftersale_id, order_id) REFERENCES aftersales(id, order_id)
);

CREATE TABLE approvals (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    order_id TEXT NOT NULL REFERENCES orders(id),
    action TEXT NOT NULL CHECK (action IN ('apply_price_protection','request_return','request_exchange')),
    args JSONB NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'pending'),
    created_at TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (order_id, user_id) REFERENCES orders(id, user_id)
);

CREATE TABLE handoffs (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    summary TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

-- Read indexes
CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_aftersales_order ON aftersales(order_id);
CREATE INDEX IF NOT EXISTS idx_refunds_order ON refunds(order_id);
CREATE INDEX IF NOT EXISTS idx_approvals_user_status ON approvals(user_id, status);

CREATE SEQUENCE users_id_seq;
CREATE SEQUENCE payment_methods_id_seq;
CREATE SEQUENCE spus_id_seq;
CREATE SEQUENCE skus_id_seq;
CREATE SEQUENCE coupons_id_seq;
CREATE SEQUENCE user_coupons_id_seq;
CREATE SEQUENCE orders_id_seq START WITH 1001;
CREATE SEQUENCE order_items_id_seq;
CREATE SEQUENCE aftersales_id_seq;
CREATE SEQUENCE refunds_id_seq;
CREATE SEQUENCE approvals_id_seq;
CREATE SEQUENCE handoffs_id_seq;

-- Keep cumulative refunds within the order's captured payment even for direct SQL.
CREATE TABLE order_refund_totals (
    order_id TEXT PRIMARY KEY REFERENCES orders(id),
    amount_cents BIGINT NOT NULL CHECK (amount_cents >= 0)
);

CREATE FUNCTION reserve_refund() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    paid BIGINT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Refund entries are append-only' USING ERRCODE = '23514';
    END IF;
    SELECT paid_cents INTO STRICT paid FROM orders WHERE id = NEW.order_id FOR UPDATE;
    INSERT INTO order_refund_totals AS totals (order_id, amount_cents)
        SELECT NEW.order_id, NEW.amount_cents WHERE NEW.amount_cents <= paid
        ON CONFLICT (order_id) DO UPDATE
        SET amount_cents = totals.amount_cents + EXCLUDED.amount_cents
        WHERE totals.amount_cents <= paid - EXCLUDED.amount_cents;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Refund exceeds captured payment' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER refunds_reserve_budget BEFORE INSERT OR UPDATE OR DELETE ON refunds
    FOR EACH ROW EXECUTE FUNCTION reserve_refund();

CREATE FUNCTION check_paid_balance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.paid_cents < (SELECT amount_cents FROM order_refund_totals WHERE order_id = OLD.id) THEN
        RAISE EXCEPTION 'Captured payment is below refunds' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER orders_check_paid BEFORE UPDATE OF paid_cents ON orders
    FOR EACH ROW EXECUTE FUNCTION check_paid_balance();

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL REFERENCES users(id),
    revision BIGINT NOT NULL CHECK (revision > 0),
    updated_at TIMESTAMPTZ NOT NULL,
    state_json JSONB NOT NULL,
    proposals_json JSONB NOT NULL,
    message_count INTEGER NOT NULL CHECK (message_count >= 0),
    trace_run BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE session_messages (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq > 0),
    message_json JSONB NOT NULL,
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE action_executions (
    execution_key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    proposal_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    tool_name TEXT NOT NULL,
    canonical_args_json JSONB NOT NULL,
    args_hash TEXT NOT NULL,
    result_json JSONB NOT NULL,
    before_snapshot_json JSONB NOT NULL,
    after_snapshot_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    execution_seq BIGINT GENERATED ALWAYS AS IDENTITY,
    UNIQUE (session_id, proposal_id, action_id)
);

CREATE INDEX idx_action_executions_session ON action_executions(session_id, execution_seq);

CREATE TABLE session_traces (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    run_id TEXT NOT NULL UNIQUE,
    run_number BIGINT NOT NULL CHECK (run_number > 0),
    PRIMARY KEY (session_id, run_number)
);

CREATE TABLE audit_traces (
    session_id TEXT NOT NULL,
    run_number BIGINT NOT NULL,
    seq BIGINT NOT NULL CHECK (seq BETWEEN 1 AND 1000000),
    timestamp TIMESTAMPTZ NOT NULL,
    event TEXT NOT NULL,
    data JSONB NOT NULL,
    PRIMARY KEY (session_id, run_number, seq),
    FOREIGN KEY (session_id, run_number) REFERENCES session_traces(session_id, run_number)
) PARTITION BY HASH (session_id);

DO $$
BEGIN
    FOR part IN 0..7 LOOP
        EXECUTE format('CREATE TABLE audit_traces_p%s PARTITION OF audit_traces
            FOR VALUES WITH (MODULUS 8, REMAINDER %s)', part, part);
    END LOOP;
END;
$$;
