ALTER TABLE control_plane.runs
    DROP CONSTRAINT IF EXISTS runs_state_check;

ALTER TABLE control_plane.runs
    ALTER COLUMN state TYPE varchar(24),
    ADD CONSTRAINT runs_state_check CHECK (state IN (
        'QUEUED', 'RUNNING', 'WAITING_FOR_INPUT',
        'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'
    ));

-- WAITING_FOR_INPUT is intentionally absent: a human pause has no execution
-- lease and must never be restarted by the generic reconciliation worker.
DROP INDEX IF EXISTS control_plane.runs_reconciliation_pending_idx;
CREATE INDEX runs_reconciliation_pending_idx
    ON control_plane.runs (next_reconcile_at, created_at, run_id)
    WHERE state IN ('QUEUED', 'RUNNING');

CREATE TABLE control_plane.run_interactions (
    tenant_id uuid NOT NULL,
    interaction_id varchar(200) NOT NULL,
    run_id uuid NOT NULL,
    kind varchar(40) NOT NULL CHECK (kind = 'clarification'),
    interaction_version bigint NOT NULL CHECK (interaction_version > 0),
    request_payload jsonb NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RESPONDING', 'RESPONDED')),
    response_idempotency_key varchar(200),
    response_fingerprint char(64),
    response_request_id uuid,
    answer text CHECK (answer IS NULL OR char_length(answer) BETWEEN 1 AND 100000),
    responded_by_issuer varchar(2048),
    responded_by_subject varchar(255),
    created_at timestamptz NOT NULL DEFAULT now(),
    responded_at timestamptz,
    PRIMARY KEY (tenant_id, interaction_id),
    UNIQUE (tenant_id, run_id, interaction_version),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES control_plane.runs (tenant_id, run_id)
        ON DELETE CASCADE,
    CHECK (
        (status = 'PENDING'
            AND response_idempotency_key IS NULL
            AND response_fingerprint IS NULL
            AND response_request_id IS NULL
            AND answer IS NULL
            AND responded_at IS NULL)
        OR
        (status IN ('RESPONDING', 'RESPONDED')
            AND response_idempotency_key IS NOT NULL
            AND response_fingerprint IS NOT NULL
            AND response_request_id IS NOT NULL
            AND answer IS NOT NULL)
    ),
    CHECK (status <> 'RESPONDED' OR responded_at IS NOT NULL)
);

GRANT SELECT, INSERT, UPDATE ON control_plane.run_interactions TO ratsnest_app;

ALTER TABLE control_plane.run_interactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.run_interactions FORCE ROW LEVEL SECURITY;
CREATE POLICY run_interactions_tenant_isolation
    ON control_plane.run_interactions
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());
