ALTER TABLE control_plane.runs
    ADD COLUMN runtime_principal_id varchar(128)
        CHECK (
            runtime_principal_id IS NULL
            OR runtime_principal_id ~ '^[A-Za-z0-9_-]{32,128}$'
        ),
    ADD COLUMN state_version bigint NOT NULL DEFAULT 0
        CHECK (state_version >= 0),
    ADD COLUMN reconcile_attempts integer NOT NULL DEFAULT 0
        CHECK (reconcile_attempts >= 0),
    ADD COLUMN reconcile_locked_by varchar(200),
    ADD COLUMN reconcile_locked_at timestamptz,
    ADD COLUMN next_reconcile_at timestamptz NOT NULL DEFAULT now();

-- Cross-tenant claim/release functions execute as the table owner. RLS remains
-- enabled for ratsnest_app, which startup validation requires to be a
-- non-owner without BYPASSRLS.
ALTER TABLE control_plane.runs NO FORCE ROW LEVEL SECURITY;

CREATE TABLE control_plane.run_outbox (
    tenant_id uuid NOT NULL,
    event_id uuid NOT NULL,
    run_id uuid NOT NULL,
    state_version bigint NOT NULL CHECK (state_version > 0),
    source_event_seq bigint CHECK (source_event_seq > 0),
    event_type varchar(100) NOT NULL CHECK (btrim(event_type) <> ''),
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    available_at timestamptz NOT NULL DEFAULT now(),
    publish_attempts integer NOT NULL DEFAULT 0 CHECK (publish_attempts >= 0),
    locked_by varchar(200),
    locked_at timestamptz,
    published_at timestamptz,
    PRIMARY KEY (event_id),
    UNIQUE (tenant_id, run_id, state_version),
    UNIQUE (tenant_id, run_id, source_event_seq),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES control_plane.runs (tenant_id, run_id)
        ON DELETE CASCADE
);

CREATE INDEX run_outbox_pending_idx
    ON control_plane.run_outbox (available_at, occurred_at, event_id)
    WHERE published_at IS NULL;

CREATE INDEX runs_reconciliation_pending_idx
    ON control_plane.runs (next_reconcile_at, created_at, run_id)
    WHERE state IN ('QUEUED', 'RUNNING');

GRANT INSERT ON control_plane.run_outbox TO ratsnest_app;

ALTER TABLE control_plane.run_outbox ENABLE ROW LEVEL SECURITY;
CREATE POLICY run_outbox_tenant_insert
    ON control_plane.run_outbox
    FOR INSERT
    TO ratsnest_app
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

-- The table is not directly readable by the application role. This narrowly
-- scoped security-definer function is the relay's only cross-tenant claim path.
CREATE FUNCTION control_plane.claim_run_outbox(
    worker_id text,
    batch_size integer
)
RETURNS TABLE (
    tenant_id uuid,
    event_id uuid,
    run_id uuid,
    state_version bigint,
    source_event_seq bigint,
    event_type text,
    payload jsonb,
    occurred_at timestamptz,
    publish_attempts integer
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, control_plane
AS $$
    WITH candidates AS (
        SELECT pending.tenant_id, pending.event_id
        FROM control_plane.run_outbox AS pending
        WHERE pending.published_at IS NULL
          AND pending.available_at <= clock_timestamp()
          AND NOT EXISTS (
              SELECT 1
              FROM control_plane.run_outbox AS earlier
              WHERE earlier.tenant_id = pending.tenant_id
                AND earlier.run_id = pending.run_id
                AND earlier.published_at IS NULL
                AND earlier.state_version < pending.state_version
          )
          AND (
              pending.locked_at IS NULL
              OR pending.locked_at < clock_timestamp() - interval '5 minutes'
          )
        ORDER BY pending.available_at, pending.occurred_at, pending.event_id
        FOR UPDATE SKIP LOCKED
        LIMIT LEAST(GREATEST(COALESCE(batch_size, 1), 1), 500)
    ), claimed AS (
        UPDATE control_plane.run_outbox AS pending
        SET locked_by = worker_id,
            locked_at = clock_timestamp(),
            publish_attempts = pending.publish_attempts + 1
        FROM candidates
        WHERE pending.tenant_id = candidates.tenant_id
          AND pending.event_id = candidates.event_id
        RETURNING pending.tenant_id, pending.event_id, pending.run_id,
                  pending.state_version, pending.source_event_seq, pending.event_type,
                  pending.payload, pending.occurred_at,
                  pending.publish_attempts
    )
    SELECT claimed.tenant_id, claimed.event_id, claimed.run_id,
           claimed.state_version, claimed.source_event_seq, claimed.event_type::text,
           claimed.payload, claimed.occurred_at, claimed.publish_attempts
    FROM claimed
    ORDER BY claimed.occurred_at, claimed.event_id;
$$;

CREATE FUNCTION control_plane.ack_run_outbox(
    claimed_event_id uuid,
    worker_id text
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, control_plane
AS $$
    WITH acknowledged AS (
        UPDATE control_plane.run_outbox
        SET published_at = clock_timestamp(), locked_by = NULL, locked_at = NULL
        WHERE event_id = claimed_event_id
          AND published_at IS NULL
          AND locked_by = worker_id
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM acknowledged);
$$;

CREATE FUNCTION control_plane.retry_run_outbox(
    claimed_event_id uuid,
    worker_id text,
    delay_seconds integer
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, control_plane
AS $$
    WITH released AS (
        UPDATE control_plane.run_outbox
        SET available_at = clock_timestamp()
                + make_interval(secs => LEAST(GREATEST(COALESCE(delay_seconds, 1), 1), 3600)),
            locked_by = NULL,
            locked_at = NULL
        WHERE event_id = claimed_event_id
          AND published_at IS NULL
          AND locked_by = worker_id
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM released);
$$;

REVOKE ALL ON FUNCTION control_plane.claim_run_outbox(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane.ack_run_outbox(uuid, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane.retry_run_outbox(uuid, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION control_plane.claim_run_outbox(text, integer) TO ratsnest_app;
GRANT EXECUTE ON FUNCTION control_plane.ack_run_outbox(uuid, text) TO ratsnest_app;
GRANT EXECUTE ON FUNCTION control_plane.retry_run_outbox(uuid, text, integer) TO ratsnest_app;

-- Reconciliation is intentionally claimed through a narrow cross-tenant
-- function. The worker must reactivate the returned tenant before reading or
-- updating the run through the normal RLS-protected repositories.
CREATE FUNCTION control_plane.claim_runs_for_reconciliation(
    worker_id text,
    batch_size integer
)
RETURNS TABLE (
    tenant_id uuid,
    run_id uuid,
    reconcile_attempts integer
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, control_plane
AS $$
    WITH candidates AS (
        SELECT pending.tenant_id, pending.run_id
        FROM control_plane.runs AS pending
        WHERE pending.state IN ('QUEUED', 'RUNNING')
          AND pending.next_reconcile_at <= clock_timestamp()
          AND (
              pending.reconcile_locked_at IS NULL
              OR pending.reconcile_locked_at < clock_timestamp() - interval '5 minutes'
          )
        ORDER BY pending.next_reconcile_at, pending.created_at, pending.run_id
        FOR UPDATE SKIP LOCKED
        LIMIT LEAST(GREATEST(COALESCE(batch_size, 1), 1), 100)
    ), claimed AS (
        UPDATE control_plane.runs AS pending
        SET reconcile_locked_by = worker_id,
            reconcile_locked_at = clock_timestamp(),
            reconcile_attempts = pending.reconcile_attempts + 1
        FROM candidates
        WHERE pending.tenant_id = candidates.tenant_id
          AND pending.run_id = candidates.run_id
        RETURNING pending.tenant_id, pending.run_id, pending.reconcile_attempts
    )
    SELECT claimed.tenant_id, claimed.run_id, claimed.reconcile_attempts
    FROM claimed
    ORDER BY claimed.run_id;
$$;

CREATE FUNCTION control_plane.release_run_reconciliation(
    claimed_tenant_id uuid,
    claimed_run_id uuid,
    worker_id text,
    delay_seconds integer
)
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, control_plane
AS $$
    WITH released AS (
        UPDATE control_plane.runs
        SET next_reconcile_at = clock_timestamp()
                + make_interval(secs => LEAST(GREATEST(COALESCE(delay_seconds, 1), 1), 3600)),
            reconcile_locked_by = NULL,
            reconcile_locked_at = NULL
        WHERE tenant_id = claimed_tenant_id
          AND run_id = claimed_run_id
          AND reconcile_locked_by = worker_id
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM released);
$$;

REVOKE ALL ON FUNCTION control_plane.claim_runs_for_reconciliation(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane.release_run_reconciliation(uuid, uuid, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION control_plane.claim_runs_for_reconciliation(text, integer) TO ratsnest_app;
GRANT EXECUTE ON FUNCTION control_plane.release_run_reconciliation(uuid, uuid, text, integer) TO ratsnest_app;
