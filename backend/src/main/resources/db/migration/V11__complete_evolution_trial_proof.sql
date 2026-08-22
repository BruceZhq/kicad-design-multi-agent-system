-- A trial snapshots every identity used by the evaluator.  Result evidence is
-- accepted only once and remains content-addressed after candidate transition.
DO $preflight$
DECLARE
    pending_count bigint;
    duplicate_workflow_count bigint;
BEGIN
    SELECT count(*) INTO pending_count
    FROM control_plane.evolution_trials
    WHERE verdict = 'PENDING';
    IF pending_count > 0 THEN
        RAISE EXCEPTION
            'V11 requires all legacy PENDING evolution trials to be drained first (found %)',
            pending_count
            USING HINT = 'Before V11, review the rows and explicitly mark obsolete V9/V10 trials CANCELLED; do not fabricate evaluation proof.';
    END IF;

    SELECT count(*) INTO duplicate_workflow_count
    FROM (
        SELECT tenant_id, temporal_workflow_id
        FROM control_plane.evolution_trials
        WHERE temporal_workflow_id IS NOT NULL
        GROUP BY tenant_id, temporal_workflow_id
        HAVING count(*) > 1
    ) duplicates;
    IF duplicate_workflow_count > 0 THEN
        RAISE EXCEPTION
            'V11 found % duplicate tenant/workflow bindings; reconcile them before migration',
            duplicate_workflow_count;
    END IF;
END
$preflight$;

ALTER TABLE control_plane.evolution_trials
    ADD COLUMN base_manifest_digest char(64),
    ADD COLUMN candidate_digest char(64),
    ADD COLUMN eval_suite_digest char(64),
    ADD COLUMN report_digest char(64),
    ADD COLUMN authoritative_report jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(authoritative_report) = 'object'),
    ADD COLUMN completed_at timestamptz;

UPDATE control_plane.evolution_trials trial
SET base_manifest_digest = candidate.base_manifest_digest,
    candidate_digest = repeat('0', 64),
    eval_suite_digest = coalesce(
        trial.optimization_suite_digest,
        repeat('0', 64)
    )
FROM control_plane.evolution_candidates candidate
WHERE candidate.tenant_id = trial.tenant_id
  AND candidate.candidate_id = trial.candidate_id;

UPDATE control_plane.evolution_trials
SET report_digest = repeat('0', 64),
    completed_at = updated_at
WHERE verdict <> 'PENDING';

ALTER TABLE control_plane.evolution_trials
    ALTER COLUMN base_manifest_digest SET NOT NULL,
    ALTER COLUMN candidate_digest SET NOT NULL,
    ALTER COLUMN eval_suite_digest SET NOT NULL,
    ADD CONSTRAINT evolution_trials_base_manifest_digest_check
        CHECK (base_manifest_digest ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT evolution_trials_candidate_digest_check
        CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT evolution_trials_eval_suite_digest_check
        CHECK (eval_suite_digest ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT evolution_trials_report_digest_check
        CHECK (report_digest IS NULL OR report_digest ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT evolution_trials_completion_check
        CHECK (
            (verdict = 'PENDING' AND report_digest IS NULL AND completed_at IS NULL)
            OR
            (verdict <> 'PENDING' AND report_digest IS NOT NULL AND completed_at IS NOT NULL)
        );

CREATE UNIQUE INDEX evolution_trials_one_pending_idx
    ON control_plane.evolution_trials (tenant_id, candidate_id)
    WHERE verdict = 'PENDING';

CREATE UNIQUE INDEX evolution_trials_workflow_id_idx
    ON control_plane.evolution_trials (tenant_id, temporal_workflow_id)
    WHERE temporal_workflow_id IS NOT NULL;
