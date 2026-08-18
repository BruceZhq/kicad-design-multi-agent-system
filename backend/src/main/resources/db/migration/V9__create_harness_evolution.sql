-- Harness versions are immutable release identities. The control plane selects
-- one from its rollout policy and snapshots it on the Run; runtime pods expose
-- the matching RATSNEST_HARNESS_VERSION_ID for cross-boundary verification.
CREATE TABLE control_plane.harness_versions (
    harness_version_id varchar(120) PRIMARY KEY CHECK (btrim(harness_version_id) <> ''),
    version varchar(80) NOT NULL UNIQUE CHECK (btrim(version) <> ''),
    parent_version_id varchar(120),
    source_commit varchar(64) NOT NULL CHECK (btrim(source_commit) <> ''),
    source_tree_digest char(64) NOT NULL CHECK (source_tree_digest ~ '^[0-9a-f]{64}$'),
    dirty boolean NOT NULL,
    runtime_image_digest varchar(71),
    toolchain_digest varchar(71),
    bundle_digest char(64) NOT NULL CHECK (bundle_digest ~ '^[0-9a-f]{64}$'),
    contract_digest char(64) NOT NULL CHECK (contract_digest ~ '^[0-9a-f]{64}$'),
    policy_digest char(64) NOT NULL CHECK (policy_digest ~ '^[0-9a-f]{64}$'),
    manifest_object_key varchar(1024),
    manifest_digest char(64) NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    release_status varchar(16) NOT NULL CHECK (release_status IN (
        'CANDIDATE', 'APPROVED', 'CANARY', 'STABLE', 'RETIRED', 'ROLLED_BACK'
    )),
    attested boolean NOT NULL DEFAULT false,
    created_by varchar(255) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    transition_reason varchar(2000),
    updated_by varchar(255),
    row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (harness_version_id, manifest_digest),
    FOREIGN KEY (parent_version_id)
        REFERENCES control_plane.harness_versions (harness_version_id),
    CHECK (runtime_image_digest IS NULL
           OR runtime_image_digest ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (toolchain_digest IS NULL
           OR toolchain_digest ~ '^(sha256:)?[0-9a-f]{64}$'),
    CHECK (
        manifest_object_key IS NULL
        OR btrim(manifest_object_key) <> ''
    ),
    CHECK (NOT attested OR (
        NOT dirty
        AND
        runtime_image_digest IS NOT NULL
        AND manifest_object_key IS NOT NULL
        AND manifest_digest IS NOT NULL
    ))
);

-- Existing runs predate version pinning. They remain auditable under one
-- explicit, non-attested legacy identity instead of receiving fabricated
-- source/image evidence.
INSERT INTO control_plane.harness_versions (
    harness_version_id, version, source_commit, source_tree_digest, dirty,
    bundle_digest, contract_digest, policy_digest, manifest_digest,
    release_status, attested, created_by,
    activated_at
) VALUES (
    'legacy-baseline',
    'legacy-baseline',
    'legacy-unversioned',
    repeat('0', 64),
    false,
    repeat('0', 64),
    repeat('0', 64),
    repeat('0', 64),
    repeat('0', 64),
    'STABLE',
    false,
    'flyway-v9',
    now()
);

GRANT SELECT, INSERT ON control_plane.harness_versions TO ratsnest_app;
GRANT UPDATE (
    release_status, transition_reason, updated_by,
    activated_at, row_version, updated_at
) ON control_plane.harness_versions TO ratsnest_app;

-- Release routing is platform-owned. Only the OIDC-gated platform release API
-- mutates this CAS row; product callers cannot choose a channel.
CREATE TABLE control_plane.harness_rollouts (
    rollout_id varchar(80) PRIMARY KEY CHECK (btrim(rollout_id) <> ''),
    stable_version_id varchar(120) NOT NULL,
    canary_version_id varchar(120),
    canary_percent integer NOT NULL DEFAULT 0 CHECK (canary_percent BETWEEN 0 AND 100),
    row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
    updated_by varchar(255) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (stable_version_id)
        REFERENCES control_plane.harness_versions (harness_version_id),
    FOREIGN KEY (canary_version_id)
        REFERENCES control_plane.harness_versions (harness_version_id),
    CHECK (canary_percent = 0 OR canary_version_id IS NOT NULL),
    CHECK (canary_version_id IS NULL OR canary_version_id <> stable_version_id)
);

INSERT INTO control_plane.harness_rollouts (
    rollout_id, stable_version_id, canary_percent, updated_by
) VALUES ('production', 'legacy-baseline', 0, 'flyway-v9');

GRANT SELECT ON control_plane.harness_rollouts TO ratsnest_app;
GRANT UPDATE (
    stable_version_id, canary_version_id, canary_percent,
    row_version, updated_by, updated_at
) ON control_plane.harness_rollouts TO ratsnest_app;

ALTER TABLE control_plane.runs
    ADD COLUMN harness_version_id varchar(120),
    ADD COLUMN harness_manifest_digest char(64),
    ADD COLUMN harness_channel varchar(16) NOT NULL DEFAULT 'stable'
        CHECK (harness_channel IN (
            'stable', 'canary', 'previous_stable', 'evaluation', 'development'
        ));

UPDATE control_plane.runs
SET harness_version_id = 'legacy-baseline',
    harness_manifest_digest = repeat('0', 64)
WHERE harness_version_id IS NULL OR harness_manifest_digest IS NULL;

ALTER TABLE control_plane.runs
    ALTER COLUMN harness_version_id SET NOT NULL,
    ALTER COLUMN harness_manifest_digest SET NOT NULL,
    ADD CONSTRAINT runs_harness_manifest_digest_check
        CHECK (harness_manifest_digest ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT runs_harness_identity_fk
        FOREIGN KEY (harness_version_id, harness_manifest_digest)
        REFERENCES control_plane.harness_versions (
            harness_version_id, manifest_digest
        );

CREATE INDEX runs_harness_version_idx
    ON control_plane.runs (tenant_id, harness_version_id, created_at DESC);

CREATE FUNCTION control_plane.reject_run_harness_identity_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.harness_version_id IS DISTINCT FROM NEW.harness_version_id
       OR OLD.harness_manifest_digest IS DISTINCT FROM NEW.harness_manifest_digest
       OR OLD.harness_channel IS DISTINCT FROM NEW.harness_channel THEN
        RAISE EXCEPTION 'A Run harness identity is immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION control_plane.reject_run_harness_identity_change() FROM PUBLIC;

CREATE TRIGGER runs_harness_identity_immutable
BEFORE UPDATE OF harness_version_id, harness_manifest_digest, harness_channel
ON control_plane.runs
FOR EACH ROW
EXECUTE FUNCTION control_plane.reject_run_harness_identity_change();

CREATE TABLE control_plane.evolution_observations (
    tenant_id uuid NOT NULL,
    observation_id char(64) NOT NULL CHECK (observation_id ~ '^[0-9a-f]{64}$'),
    run_id uuid NOT NULL,
    source_event_seq bigint NOT NULL CHECK (source_event_seq >= 0),
    harness_version_id varchar(120) NOT NULL,
    harness_channel varchar(16) NOT NULL CHECK (harness_channel IN (
        'stable', 'canary', 'previous_stable', 'evaluation', 'development'
    )),
    harness_manifest_digest char(64) NOT NULL
        CHECK (harness_manifest_digest ~ '^[0-9a-f]{64}$'),
    profile_reference varchar(120) NOT NULL,
    profile_digest char(64) NOT NULL CHECK (profile_digest ~ '^[0-9a-f]{64}$'),
    scope_fingerprint char(64) NOT NULL CHECK (scope_fingerprint ~ '^[0-9a-f]{64}$'),
    project_fingerprint char(64) NOT NULL CHECK (project_fingerprint ~ '^[0-9a-f]{64}$'),
    event_type varchar(64) NOT NULL CHECK (event_type ~ '^[a-z][a-z0-9_]{1,63}$'),
    failure_signature varchar(128),
    step varchar(120) NOT NULL,
    check_name varchar(200),
    category varchar(80),
    recoverability varchar(80),
    strategy varchar(160),
    required_capability varchar(160),
    outcome varchar(32) NOT NULL CHECK (outcome IN (
        'observed', 'resolved', 'improved', 'verified', 'rejected',
        'error', 'hard_conflict'
    )),
    revision integer NOT NULL CHECK (revision >= 0),
    evidence_digest char(64) NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, observation_id),
    UNIQUE (tenant_id, run_id, source_event_seq),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES control_plane.runs (tenant_id, run_id)
        ON DELETE CASCADE,
    FOREIGN KEY (harness_version_id, harness_manifest_digest)
        REFERENCES control_plane.harness_versions (
            harness_version_id, manifest_digest
        )
);

CREATE INDEX evolution_observations_signature_idx
    ON control_plane.evolution_observations (
        tenant_id, failure_signature, observed_at DESC, observation_id
    ) WHERE failure_signature IS NOT NULL;

CREATE TABLE control_plane.evolution_candidates (
    tenant_id uuid NOT NULL,
    candidate_id char(64) NOT NULL CHECK (candidate_id ~ '^[0-9a-f]{64}$'),
    base_harness_version_id varchar(120) NOT NULL,
    base_manifest_digest char(64) NOT NULL
        CHECK (base_manifest_digest ~ '^[0-9a-f]{64}$'),
    failure_signature varchar(128) NOT NULL,
    step varchar(120) NOT NULL,
    check_name varchar(200),
    category varchar(80),
    required_capability varchar(160),
    profile_references jsonb NOT NULL CHECK (
        jsonb_typeof(profile_references) = 'array'
        AND jsonb_array_length(profile_references) BETWEEN 1 AND 16
    ),
    observation_ids jsonb NOT NULL CHECK (
        jsonb_typeof(observation_ids) = 'array'
        AND jsonb_array_length(observation_ids) BETWEEN 1 AND 10000
    ),
    occurrence_count integer NOT NULL CHECK (occurrence_count > 0),
    project_count integer NOT NULL CHECK (project_count > 0),
    risk_tier varchar(16) NOT NULL CHECK (risk_tier IN (
        'low', 'medium', 'high', 'prohibited'
    )),
    change_kind varchar(32) NOT NULL CHECK (change_kind IN (
        'unclassified', 'prompt', 'skill', 'tool_description', 'policy',
        'router', 'parser', 'tool_adapter', 'recovery_strategy',
        'validator', 'harness_code'
    )),
    status varchar(32) NOT NULL CHECK (status IN (
        'observed', 'eligible', 'evaluating', 'awaiting_approval',
        'approved', 'canary', 'promoted', 'rejected', 'rolled_back', 'stale'
    )),
    transition_reason varchar(2000),
    updated_by_issuer varchar(2048),
    updated_by_subject varchar(255),
    row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, candidate_id),
    UNIQUE (tenant_id, base_harness_version_id, base_manifest_digest, failure_signature),
    FOREIGN KEY (base_harness_version_id)
        REFERENCES control_plane.harness_versions (harness_version_id)
);

CREATE INDEX evolution_candidates_status_idx
    ON control_plane.evolution_candidates (
        tenant_id, status, updated_at DESC, candidate_id
    );

CREATE TABLE control_plane.evolution_trials (
    tenant_id uuid NOT NULL,
    trial_id uuid NOT NULL,
    candidate_id char(64) NOT NULL,
    attempt integer NOT NULL CHECK (attempt BETWEEN 1 AND 100),
    input_digest char(64) NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
    temporal_workflow_id varchar(255),
    patch_commit varchar(64),
    patch_sha256 char(64),
    candidate_image_digest varchar(71),
    optimization_suite_digest char(64),
    holdout_suite_digest char(64),
    adversarial_suite_digest char(64),
    baseline_metrics jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(baseline_metrics) = 'object'),
    candidate_metrics jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(candidate_metrics) = 'object'),
    guardrail_results jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(guardrail_results) = 'object'),
    verdict varchar(32) NOT NULL CHECK (verdict IN (
        'PENDING', 'PASSED', 'FAILED', 'REGRESSION', 'POLICY_REJECTED',
        'ENVIRONMENT_ISSUE', 'CANCELLED'
    )),
    report_object_key varchar(1024),
    llm_tokens bigint NOT NULL DEFAULT 0 CHECK (llm_tokens >= 0),
    wall_clock_ms bigint NOT NULL DEFAULT 0 CHECK (wall_clock_ms >= 0),
    row_version bigint NOT NULL DEFAULT 1 CHECK (row_version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, trial_id),
    UNIQUE (tenant_id, candidate_id, attempt),
    FOREIGN KEY (tenant_id, candidate_id)
        REFERENCES control_plane.evolution_candidates (tenant_id, candidate_id)
        ON DELETE CASCADE,
    CHECK (patch_sha256 IS NULL OR patch_sha256 ~ '^[0-9a-f]{64}$'),
    CHECK (candidate_image_digest IS NULL
           OR candidate_image_digest ~ '^sha256:[0-9a-f]{64}$'),
    CHECK (optimization_suite_digest IS NULL
           OR optimization_suite_digest ~ '^[0-9a-f]{64}$'),
    CHECK (holdout_suite_digest IS NULL
           OR holdout_suite_digest ~ '^[0-9a-f]{64}$'),
    CHECK (adversarial_suite_digest IS NULL
           OR adversarial_suite_digest ~ '^[0-9a-f]{64}$')
);

CREATE INDEX evolution_trials_candidate_idx
    ON control_plane.evolution_trials (
        tenant_id, candidate_id, attempt DESC
    );

GRANT SELECT, INSERT ON control_plane.evolution_observations TO ratsnest_app;
GRANT SELECT, INSERT, UPDATE ON control_plane.evolution_candidates TO ratsnest_app;
GRANT SELECT, INSERT, UPDATE ON control_plane.evolution_trials TO ratsnest_app;

ALTER TABLE control_plane.evolution_observations ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.evolution_observations FORCE ROW LEVEL SECURITY;
CREATE POLICY evolution_observations_tenant_isolation
    ON control_plane.evolution_observations
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

ALTER TABLE control_plane.evolution_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.evolution_candidates FORCE ROW LEVEL SECURITY;
CREATE POLICY evolution_candidates_tenant_isolation
    ON control_plane.evolution_candidates
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

ALTER TABLE control_plane.evolution_trials ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.evolution_trials FORCE ROW LEVEL SECURITY;
CREATE POLICY evolution_trials_tenant_isolation
    ON control_plane.evolution_trials
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());
