ALTER TABLE control_plane.runs
    ADD COLUMN forked_from_run_id uuid,
    ADD CONSTRAINT runs_forked_from_fk
        FOREIGN KEY (tenant_id, forked_from_run_id)
        REFERENCES control_plane.runs (tenant_id, run_id)
        DEFERRABLE INITIALLY DEFERRED,
    ADD CONSTRAINT runs_fork_provenance_shape CHECK (
        forked_from_run_id IS NULL
        OR (
            revision_number = 1
            AND parent_run_id IS NULL
            AND root_run_id = run_id
            AND forked_from_run_id <> run_id
        )
    );

CREATE INDEX runs_forked_from_idx
    ON control_plane.runs (tenant_id, forked_from_run_id)
    WHERE forked_from_run_id IS NOT NULL;

CREATE OR REPLACE FUNCTION control_plane.protect_run_revision_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.root_run_id IS DISTINCT FROM OLD.root_run_id
       OR NEW.parent_run_id IS DISTINCT FROM OLD.parent_run_id
       OR NEW.forked_from_run_id IS DISTINCT FROM OLD.forked_from_run_id
       OR NEW.revision_number IS DISTINCT FROM OLD.revision_number THEN
        RAISE EXCEPTION 'run revision and fork identity is immutable';
    END IF;
    IF OLD.delivery_status IS NOT NULL
       AND NEW.delivery_status IS DISTINCT FROM OLD.delivery_status THEN
        RAISE EXCEPTION 'run delivery status is immutable';
    END IF;
    RETURN NEW;
END;
$$;
