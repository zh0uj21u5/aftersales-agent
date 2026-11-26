-- Existing version-1 rows retain their historic terminal state.
ALTER TABLE action_executions ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE action_executions ADD CHECK (session_id IS NOT NULL OR proposal_id = 'approval');
ALTER TABLE sessions ADD UNIQUE (session_id, owner_user_id);

ALTER TABLE refunds DROP CONSTRAINT refunds_status_check,
    ADD CHECK (status IN ('pending','processing','success','failed')),
    ADD COLUMN status_changed_at TIMESTAMPTZ,
    ADD COLUMN execution_key TEXT REFERENCES action_executions(execution_key)
        DEFERRABLE INITIALLY DEFERRED,
    ADD CHECK (execution_key IS NOT NULL OR status = 'success');
CREATE INDEX idx_refunds_execution_key ON refunds(execution_key) WHERE execution_key IS NOT NULL;
CREATE INDEX idx_refunds_pending ON refunds(status_changed_at) WHERE status IN ('pending','processing');

ALTER TABLE aftersales DROP CONSTRAINT aftersales_status_check,
    ADD CHECK (status IN ('processing','completed','awaiting_payment','failed'));

ALTER TABLE approvals DROP CONSTRAINT approvals_status_check,
    ADD CHECK (status IN ('pending','approved','rejected')),
    ADD COLUMN session_id TEXT,
    ADD FOREIGN KEY (session_id, user_id) REFERENCES sessions(session_id, owner_user_id),
    ADD COLUMN requested_amount_cents BIGINT CHECK (requested_amount_cents >= 0),
    ADD COLUMN reviewer_id TEXT,
    ADD COLUMN review_note TEXT,
    ADD COLUMN reviewed_at TIMESTAMPTZ,
    ADD CHECK ((status = 'pending' AND reviewer_id IS NULL AND reviewed_at IS NULL)
        OR (status <> 'pending' AND reviewer_id IS NOT NULL AND reviewed_at IS NOT NULL));
CREATE INDEX idx_approvals_queue ON approvals(status, id);

ALTER TABLE handoffs ADD COLUMN status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','claimed','closed')),
    ADD COLUMN assignee_id TEXT,
    ADD COLUMN closed_at TIMESTAMPTZ,
    ADD COLUMN resolution TEXT,
    ADD COLUMN dedup_key TEXT UNIQUE;
CREATE INDEX idx_handoffs_queue ON handoffs(status, id);

CREATE TABLE outbox_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    execution_key TEXT NOT NULL UNIQUE REFERENCES action_executions(execution_key)
        DEFERRABLE INITIALLY DEFERRED,
    order_id TEXT NOT NULL REFERENCES orders(id),
    topic TEXT NOT NULL CHECK (topic IN ('refund','cancel_order','modify_address')),
    payload_json JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','dead_letter')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_retry_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    lease_token TEXT,
    lease_until TIMESTAMPTZ,
    fulfillment_done_at TIMESTAMPTZ,
    last_error TEXT,
    CHECK ((status = 'processing') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX idx_outbox_claim ON outbox_events(status, next_retry_at, id);
CREATE INDEX idx_outbox_order ON outbox_events(order_id, id);

CREATE TABLE webhook_receipts (
    event_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    refund_id TEXT NOT NULL REFERENCES refunds(id),
    received_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE reconciliation_discrepancies (
    kind TEXT NOT NULL CHECK (kind IN ('long_payment','short_payment','amount_mismatch','terminal_conflict')),
    refund_id TEXT NOT NULL,
    order_id TEXT REFERENCES orders(id),
    local_cents BIGINT CHECK (local_cents >= 0),
    channel_cents BIGINT CHECK (channel_cents >= 0),
    created_at TIMESTAMPTZ NOT NULL,
    resolved_at TIMESTAMPTZ,
    PRIMARY KEY (kind, refund_id)
);
CREATE INDEX idx_reconciliation_block ON reconciliation_discrepancies(order_id)
    WHERE resolved_at IS NULL;

CREATE TABLE session_notifications (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    notification_key TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (session_id, notification_key)
);
CREATE INDEX idx_session_notices ON session_notifications(session_id, id DESC);

CREATE OR REPLACE FUNCTION reserve_refund() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    paid BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Refund entries cannot be deleted' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.id, NEW.order_id, NEW.aftersale_id, NEW.amount_cents, NEW.payment_method_id,
               NEW.execution_key, NEW.created_at)
            IS DISTINCT FROM
           ROW(OLD.id, OLD.order_id, OLD.aftersale_id, OLD.amount_cents, OLD.payment_method_id,
               OLD.execution_key, OLD.created_at)
            OR NOT ((OLD.status = 'pending' AND NEW.status = 'processing')
                OR (OLD.status = 'processing' AND NEW.status IN ('success','failed')))
            OR NEW.status_changed_at IS NULL
            OR NEW.status_changed_at < coalesce(OLD.status_changed_at, OLD.created_at) THEN
            RAISE EXCEPTION 'Invalid refund state transition' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.status NOT IN ('pending','success') THEN
        RAISE EXCEPTION 'Invalid initial refund state' USING ERRCODE = '23514';
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
