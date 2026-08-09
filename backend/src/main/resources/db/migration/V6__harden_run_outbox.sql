-- Serialize creation and publication per run. The relay remains at-least-once:
-- a broker ACK followed by a process crash may resend the same immutable
-- event_id, which consumers use as their deduplication key.

REVOKE INSERT ON control_plane.run_outbox FROM ratsnest_app;

CREATE FUNCTION control_plane.append_run_outbox(
    requested_tenant_id uuid,
    requested_event_id uuid,
    requested_run_id uuid,
    requested_source_event_seq bigint,
    requested_event_type text,
    requested_payload jsonb
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, control_plane
AS $$
DECLARE
    next_state_version bigint;
BEGIN
    IF requested_tenant_id IS DISTINCT FROM control_plane.current_tenant_id() THEN
        RAISE EXCEPTION 'outbox tenant does not match the active tenant'
            USING ERRCODE = '42501';
    END IF;
    IF requested_event_id IS NULL OR requested_run_id IS NULL THEN
        RAISE EXCEPTION 'outbox event and run identifiers are required';
    END IF;
    IF requested_source_event_seq IS NOT NULL AND requested_source_event_seq <= 0 THEN
        RAISE EXCEPTION 'source event sequence must be positive';
    END IF;
    IF requested_event_type IS NULL OR btrim(requested_event_type) = '' THEN
        RAISE EXCEPTION 'outbox event type is required';
    END IF;
    IF requested_payload IS NULL THEN
        RAISE EXCEPTION 'outbox payload is required';
    END IF;

    -- The run row is the per-run sequence lock. A concurrent duplicate waits
    -- here, then observes the first transaction before allocating a version.
    SELECT run.state_version + 1
    INTO next_state_version
    FROM control_plane.runs AS run
    WHERE run.tenant_id = requested_tenant_id
      AND run.run_id = requested_run_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF requested_source_event_seq IS NOT NULL AND EXISTS (
        SELECT 1
        FROM control_plane.run_outbox AS existing
        WHERE existing.tenant_id = requested_tenant_id
          AND existing.run_id = requested_run_id
          AND existing.source_event_seq = requested_source_event_seq
    ) THEN
        RETURN false;
    END IF;

    UPDATE control_plane.runs
    SET state_version = next_state_version
    WHERE tenant_id = requested_tenant_id
      AND run_id = requested_run_id;

    INSERT INTO control_plane.run_outbox (
        tenant_id, event_id, run_id, state_version, source_event_seq,
        event_type, payload
    ) VALUES (
        requested_tenant_id, requested_event_id, requested_run_id,
        next_state_version, requested_source_event_seq,
        requested_event_type, requested_payload
    );

    RETURN true;
END;
$$;

REVOKE ALL ON FUNCTION control_plane.append_run_outbox(
    uuid, uuid, uuid, bigint, text, jsonb
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION control_plane.append_run_outbox(
    uuid, uuid, uuid, bigint, text, jsonb
) TO ratsnest_app;

-- Publication leases are disposable. Migrations run with publishers stopped,
-- so release any abandoned pre-V6 lease before installing the invariant.
UPDATE control_plane.run_outbox
SET locked_by = NULL, locked_at = NULL
WHERE published_at IS NULL AND locked_by IS NOT NULL;

CREATE UNIQUE INDEX run_outbox_one_active_claim_per_run_idx
    ON control_plane.run_outbox (tenant_id, run_id)
    WHERE published_at IS NULL AND locked_by IS NOT NULL;

CREATE OR REPLACE FUNCTION control_plane.claim_run_outbox(
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
    WITH heads AS MATERIALIZED (
        SELECT DISTINCT ON (pending.tenant_id, pending.run_id)
               pending.tenant_id, pending.run_id, pending.event_id
        FROM control_plane.run_outbox AS pending
        WHERE pending.published_at IS NULL
        ORDER BY pending.tenant_id, pending.run_id, pending.state_version
    ), candidates AS (
        SELECT pending.tenant_id, pending.event_id
        FROM control_plane.run_outbox AS pending
        JOIN heads
          ON heads.tenant_id = pending.tenant_id
         AND heads.run_id = pending.run_id
         AND heads.event_id = pending.event_id
        WHERE pending.available_at <= clock_timestamp()
          AND (
              pending.locked_at IS NULL
              OR pending.locked_at < clock_timestamp() - interval '5 minutes'
          )
        ORDER BY pending.available_at, pending.occurred_at, pending.event_id
        FOR UPDATE OF pending SKIP LOCKED
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
                  pending.state_version, pending.source_event_seq,
                  pending.event_type, pending.payload, pending.occurred_at,
                  pending.publish_attempts
    )
    SELECT claimed.tenant_id, claimed.event_id, claimed.run_id,
           claimed.state_version, claimed.source_event_seq,
           claimed.event_type::text, claimed.payload, claimed.occurred_at,
           claimed.publish_attempts
    FROM claimed
    ORDER BY claimed.occurred_at, claimed.event_id;
$$;

