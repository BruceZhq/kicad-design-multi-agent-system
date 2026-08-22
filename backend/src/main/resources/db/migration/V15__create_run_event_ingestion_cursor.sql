CREATE TABLE control_plane.run_event_ingestion (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    last_event_seq bigint NOT NULL DEFAULT 0 CHECK (last_event_seq >= 0),
    next_ingest_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ingest_locked_by varchar(255),
    ingest_locked_at timestamptz,
    ingest_attempts integer NOT NULL DEFAULT 0 CHECK (ingest_attempts >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, run_id),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES control_plane.runs (tenant_id, run_id)
        ON DELETE CASCADE,
    CHECK (
        (ingest_locked_by IS NULL AND ingest_locked_at IS NULL)
        OR (ingest_locked_by IS NOT NULL AND ingest_locked_at IS NOT NULL)
    )
);

-- Evolution collection starts at V15 deployment for historical terminal runs.
-- Their Runtime buffers may already have expired, so a migration must not create
-- an unbounded replay-gap retry storm while pretending historical governance was
-- reconstructed. Existing active work is deliberately replayed from zero.
INSERT INTO control_plane.run_event_ingestion (tenant_id, run_id, last_event_seq)
SELECT tenant_id,
       run_id,
       CASE
           WHEN state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT')
               THEN COALESCE(newest_event_id, 0)
           ELSE 0
       END
FROM control_plane.runs
ON CONFLICT DO NOTHING;

CREATE INDEX run_event_ingestion_pending_idx
    ON control_plane.run_event_ingestion (next_ingest_at, created_at, run_id);

GRANT SELECT, UPDATE ON control_plane.run_event_ingestion TO ratsnest_app;

ALTER TABLE control_plane.run_event_ingestion ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.run_event_ingestion FORCE ROW LEVEL SECURITY;
CREATE POLICY run_event_ingestion_tenant_isolation
    ON control_plane.run_event_ingestion
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

CREATE FUNCTION control_plane.initialize_run_event_ingestion()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, control_plane
AS $$
BEGIN
    INSERT INTO control_plane.run_event_ingestion (tenant_id, run_id)
    VALUES (NEW.tenant_id, NEW.run_id)
    ON CONFLICT DO NOTHING;
    RETURN NEW;
END;
$$;

CREATE TRIGGER runs_initialize_event_ingestion
AFTER INSERT ON control_plane.runs
FOR EACH ROW
EXECUTE FUNCTION control_plane.initialize_run_event_ingestion();

CREATE FUNCTION control_plane.claim_run_event_ingestion(
    worker_id text,
    batch_size integer
)
RETURNS TABLE (
    tenant_id uuid,
    run_id uuid,
    last_event_seq bigint,
    ingest_attempts integer
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = pg_catalog, control_plane
AS $$
    WITH candidates AS (
        SELECT pending.tenant_id, pending.run_id
        FROM control_plane.run_event_ingestion AS pending
        JOIN control_plane.runs AS run
          ON run.tenant_id = pending.tenant_id
         AND run.run_id = pending.run_id
        WHERE pending.next_ingest_at <= clock_timestamp()
          AND (
              pending.ingest_locked_at IS NULL
              OR pending.ingest_locked_at < clock_timestamp() - interval '5 minutes'
          )
          AND (
              run.state IN ('QUEUED', 'RUNNING')
              OR pending.last_event_seq < COALESCE(run.newest_event_id, 0)
          )
        ORDER BY pending.next_ingest_at, pending.created_at, pending.run_id
        FOR UPDATE OF pending SKIP LOCKED
        LIMIT LEAST(GREATEST(COALESCE(batch_size, 1), 1), 100)
    ), claimed AS (
        UPDATE control_plane.run_event_ingestion AS pending
        SET ingest_locked_by = worker_id,
            ingest_locked_at = clock_timestamp(),
            ingest_attempts = pending.ingest_attempts + 1
        FROM candidates
        WHERE pending.tenant_id = candidates.tenant_id
          AND pending.run_id = candidates.run_id
        RETURNING pending.tenant_id, pending.run_id,
                  pending.last_event_seq, pending.ingest_attempts
    )
    SELECT claimed.tenant_id, claimed.run_id,
           claimed.last_event_seq, claimed.ingest_attempts
    FROM claimed
    ORDER BY claimed.run_id;
$$;

CREATE FUNCTION control_plane.release_run_event_ingestion(
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
        UPDATE control_plane.run_event_ingestion
        SET next_ingest_at = clock_timestamp()
                + make_interval(secs => LEAST(GREATEST(COALESCE(delay_seconds, 1), 1), 3600)),
            ingest_locked_by = NULL,
            ingest_locked_at = NULL,
            updated_at = clock_timestamp()
        WHERE tenant_id = claimed_tenant_id
          AND run_id = claimed_run_id
          AND ingest_locked_by = worker_id
        RETURNING 1
    )
    SELECT EXISTS (SELECT 1 FROM released);
$$;

REVOKE ALL ON FUNCTION control_plane.initialize_run_event_ingestion() FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane.claim_run_event_ingestion(text, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane.release_run_event_ingestion(
    uuid, uuid, text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION control_plane.claim_run_event_ingestion(
    text, integer) TO ratsnest_app;
GRANT EXECUTE ON FUNCTION control_plane.release_run_event_ingestion(
    uuid, uuid, text, integer) TO ratsnest_app;
