CREATE FUNCTION control_plane.current_principal_issuer()
RETURNS text
LANGUAGE sql
STABLE
PARALLEL SAFE
RETURN NULLIF(current_setting('ratsnest.principal_issuer', true), '');

CREATE FUNCTION control_plane.current_principal_subject()
RETURNS text
LANGUAGE sql
STABLE
PARALLEL SAFE
RETURN NULLIF(current_setting('ratsnest.principal_subject', true), '');

REVOKE ALL ON FUNCTION control_plane.current_principal_issuer() FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane.current_principal_subject() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION control_plane.current_principal_issuer() TO ratsnest_app;
GRANT EXECUTE ON FUNCTION control_plane.current_principal_subject() TO ratsnest_app;

DROP POLICY memberships_tenant_isolation ON control_plane.memberships;

CREATE POLICY memberships_select
    ON control_plane.memberships
    FOR SELECT
    TO ratsnest_app
    USING (
        tenant_id = control_plane.current_tenant_id()
        OR (
            issuer = control_plane.current_principal_issuer()
            AND subject = control_plane.current_principal_subject()
        )
    );

CREATE POLICY memberships_insert
    ON control_plane.memberships
    FOR INSERT
    TO ratsnest_app
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

CREATE POLICY memberships_update
    ON control_plane.memberships
    FOR UPDATE
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

CREATE TABLE control_plane.runs (
    tenant_id uuid NOT NULL,
    run_id uuid NOT NULL,
    project_id uuid NOT NULL,
    thread_id varchar(200) NOT NULL,
    idempotency_key varchar(200) NOT NULL,
    request_fingerprint char(64) NOT NULL,
    message text NOT NULL CHECK (char_length(message) BETWEEN 1 AND 100000),
    model varchar(200),
    runtime_config jsonb NOT NULL DEFAULT '{}'::jsonb,
    state varchar(16) NOT NULL
        CHECK (state IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT')),
    runtime_run_id varchar(200),
    event_count bigint NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    oldest_event_id bigint,
    newest_event_id bigint,
    error_code varchar(100),
    error text,
    created_by_issuer varchar(2048) NOT NULL,
    created_by_subject varchar(255) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    PRIMARY KEY (tenant_id, run_id),
    UNIQUE (tenant_id, project_id, idempotency_key),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES control_plane.projects (tenant_id, project_id)
        ON DELETE CASCADE
);

CREATE INDEX runs_project_created_idx
    ON control_plane.runs (tenant_id, project_id, created_at DESC, run_id);

GRANT SELECT, INSERT, UPDATE ON control_plane.runs TO ratsnest_app;

ALTER TABLE control_plane.runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.runs FORCE ROW LEVEL SECURITY;
CREATE POLICY runs_tenant_isolation
    ON control_plane.runs
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());
