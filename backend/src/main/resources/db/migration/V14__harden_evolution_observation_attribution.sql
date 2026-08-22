ALTER TABLE control_plane.evolution_observations
    ADD COLUMN failure_origin varchar(64),
    ADD COLUMN attribution_action varchar(64),
    ADD COLUMN attribution_reason_code varchar(128),
    ADD COLUMN attribution_origin varchar(64),
    ADD COLUMN independent_project_count integer,
    ADD COLUMN independent_run_count integer,
    ADD CONSTRAINT evolution_observations_failure_origin_format CHECK (
        failure_origin IS NULL
        OR failure_origin ~ '^[a-z][a-z0-9_]{1,63}$'
    ),
    ADD CONSTRAINT evolution_observations_attribution_action_format CHECK (
        attribution_action IS NULL
        OR attribution_action ~ '^[a-z][a-z0-9_]{1,63}$'
    ),
    ADD CONSTRAINT evolution_observations_attribution_reason_format CHECK (
        attribution_reason_code IS NULL
        OR attribution_reason_code ~ '^[a-z][a-z0-9_]{1,127}$'
    ),
    ADD CONSTRAINT evolution_observations_attribution_origin_format CHECK (
        attribution_origin IS NULL
        OR attribution_origin ~ '^[a-z][a-z0-9_]{1,63}$'
    ),
    ADD CONSTRAINT evolution_observations_project_count_positive CHECK (
        independent_project_count IS NULL OR independent_project_count > 0
    ),
    ADD CONSTRAINT evolution_observations_run_count_positive CHECK (
        independent_run_count IS NULL OR independent_run_count > 0
    ),
    ADD CONSTRAINT evolution_observations_attribution_complete CHECK (
        (attribution_action IS NULL
         AND attribution_reason_code IS NULL
         AND attribution_origin IS NULL
         AND independent_project_count IS NULL
         AND independent_run_count IS NULL)
        OR
        (attribution_action IS NOT NULL
         AND attribution_reason_code IS NOT NULL
         AND attribution_origin IS NOT NULL
         AND independent_project_count IS NOT NULL
         AND independent_run_count IS NOT NULL)
    );

CREATE INDEX evolution_observations_governed_recurrence_idx
    ON control_plane.evolution_observations (
        tenant_id, harness_version_id, harness_manifest_digest,
        failure_signature, event_type, project_fingerprint, recorded_at
    )
    WHERE failure_origin = 'harness';
