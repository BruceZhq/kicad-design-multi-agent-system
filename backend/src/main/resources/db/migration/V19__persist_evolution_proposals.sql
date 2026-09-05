-- An idempotency key identifies exactly one optimizer input and immutable output.
-- A pending row is intentionally fail-closed after an ambiguous runtime call:
-- retrying cannot invoke the LLM again and silently produce a different patch.
CREATE TABLE control_plane.evolution_proposals (
    tenant_id uuid NOT NULL,
    proposal_id char(64) NOT NULL CHECK (proposal_id ~ '^[0-9a-f]{64}$'),
    candidate_id char(64) NOT NULL,
    base_manifest_digest char(64) NOT NULL
        CHECK (base_manifest_digest ~ '^[0-9a-f]{64}$'),
    request_fingerprint char(64) NOT NULL
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    request jsonb NOT NULL CHECK (jsonb_typeof(request) = 'object'),
    proposal_digest char(64) CHECK (proposal_digest ~ '^[0-9a-f]{64}$'),
    response jsonb CHECK (response IS NULL OR jsonb_typeof(response) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, proposal_id),
    FOREIGN KEY (tenant_id, candidate_id)
        REFERENCES control_plane.evolution_candidates (tenant_id, candidate_id)
        ON DELETE CASCADE,
    CHECK ((proposal_digest IS NULL AND response IS NULL AND completed_at IS NULL)
        OR (proposal_digest IS NOT NULL AND response IS NOT NULL AND completed_at IS NOT NULL))
);

CREATE FUNCTION control_plane.reject_evolution_proposal_rewrite()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.proposal_digest IS NOT NULL
       OR OLD.proposal_id IS DISTINCT FROM NEW.proposal_id
       OR OLD.candidate_id IS DISTINCT FROM NEW.candidate_id
       OR OLD.base_manifest_digest IS DISTINCT FROM NEW.base_manifest_digest
       OR OLD.request_fingerprint IS DISTINCT FROM NEW.request_fingerprint
       OR OLD.request IS DISTINCT FROM NEW.request THEN
        RAISE EXCEPTION 'An evolution proposal identity/result is immutable'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

REVOKE ALL ON FUNCTION control_plane.reject_evolution_proposal_rewrite() FROM PUBLIC;

CREATE TRIGGER evolution_proposals_immutable
BEFORE UPDATE ON control_plane.evolution_proposals
FOR EACH ROW EXECUTE FUNCTION control_plane.reject_evolution_proposal_rewrite();

GRANT SELECT, INSERT ON control_plane.evolution_proposals TO ratsnest_app;
GRANT UPDATE (proposal_digest, response, completed_at)
    ON control_plane.evolution_proposals TO ratsnest_app;

ALTER TABLE control_plane.evolution_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.evolution_proposals FORCE ROW LEVEL SECURITY;
CREATE POLICY evolution_proposals_tenant_isolation
    ON control_plane.evolution_proposals
    FOR ALL TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());
