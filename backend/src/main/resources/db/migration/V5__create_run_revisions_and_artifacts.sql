ALTER TABLE control_plane.runs
    ADD COLUMN root_run_id uuid,
    ADD COLUMN parent_run_id uuid,
    ADD COLUMN revision_number integer,
    ADD COLUMN delivery_status varchar(32)
        CHECK (delivery_status IN (
            'execution_blocked',
            'delivered_with_issues',
            'release_ready'
        ));

UPDATE control_plane.runs
SET root_run_id = run_id, revision_number = 1;

ALTER TABLE control_plane.runs
    ALTER COLUMN root_run_id SET NOT NULL,
    ALTER COLUMN revision_number SET NOT NULL,
    ADD CONSTRAINT runs_revision_number_positive CHECK (revision_number > 0),
    ADD CONSTRAINT runs_root_fk
        FOREIGN KEY (tenant_id, root_run_id)
        REFERENCES control_plane.runs (tenant_id, run_id)
        DEFERRABLE INITIALLY DEFERRED,
    ADD CONSTRAINT runs_parent_fk
        FOREIGN KEY (tenant_id, parent_run_id)
        REFERENCES control_plane.runs (tenant_id, run_id)
        DEFERRABLE INITIALLY DEFERRED,
    ADD CONSTRAINT runs_revision_unique
        UNIQUE (tenant_id, root_run_id, revision_number),
    ADD CONSTRAINT runs_revision_shape CHECK (
        (revision_number = 1 AND parent_run_id IS NULL AND root_run_id = run_id)
        OR
        (revision_number > 1 AND parent_run_id IS NOT NULL AND root_run_id <> run_id)
    );

CREATE INDEX runs_revision_parent_idx
    ON control_plane.runs (tenant_id, parent_run_id)
    WHERE parent_run_id IS NOT NULL;

CREATE TABLE control_plane.artifact_manifests (
    tenant_id uuid NOT NULL,
    manifest_id uuid NOT NULL,
    run_id uuid NOT NULL,
    source_event_seq bigint CHECK (source_event_seq > 0),
    delivery_status varchar(32) NOT NULL
        CHECK (delivery_status IN (
            'execution_blocked',
            'delivered_with_issues',
            'release_ready'
        )),
    manifest_digest char(64) NOT NULL CHECK (manifest_digest ~ '^[0-9a-f]{64}$'),
    trusted boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, manifest_id),
    UNIQUE (tenant_id, run_id),
    UNIQUE (tenant_id, run_id, source_event_seq),
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES control_plane.runs (tenant_id, run_id)
        ON DELETE CASCADE,
    CHECK (delivery_status <> 'release_ready' OR trusted)
);

CREATE TABLE control_plane.artifacts (
    tenant_id uuid NOT NULL,
    artifact_id uuid NOT NULL,
    manifest_id uuid NOT NULL,
    run_id uuid NOT NULL,
    name varchar(255) NOT NULL CHECK (name = btrim(name) AND name <> ''),
    kind varchar(80) NOT NULL CHECK (kind ~ '^[a-z0-9][a-z0-9._-]{0,79}$'),
    media_type varchar(255) NOT NULL CHECK (media_type = btrim(media_type) AND media_type <> ''),
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    object_key varchar(1024) NOT NULL CHECK (object_key = btrim(object_key) AND object_key <> ''),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, artifact_id),
    UNIQUE (tenant_id, manifest_id, name),
    UNIQUE (tenant_id, object_key),
    FOREIGN KEY (tenant_id, manifest_id)
        REFERENCES control_plane.artifact_manifests (tenant_id, manifest_id)
        ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, run_id)
        REFERENCES control_plane.runs (tenant_id, run_id)
        ON DELETE CASCADE
);

GRANT SELECT, INSERT ON control_plane.artifact_manifests TO ratsnest_app;
GRANT SELECT, INSERT ON control_plane.artifacts TO ratsnest_app;

ALTER TABLE control_plane.artifact_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.artifact_manifests FORCE ROW LEVEL SECURITY;
CREATE POLICY artifact_manifests_tenant_isolation
    ON control_plane.artifact_manifests
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

ALTER TABLE control_plane.artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.artifacts FORCE ROW LEVEL SECURITY;
CREATE POLICY artifacts_tenant_isolation
    ON control_plane.artifacts
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

CREATE FUNCTION control_plane.protect_run_revision_identity()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.root_run_id IS DISTINCT FROM OLD.root_run_id
       OR NEW.parent_run_id IS DISTINCT FROM OLD.parent_run_id
       OR NEW.revision_number IS DISTINCT FROM OLD.revision_number THEN
        RAISE EXCEPTION 'run revision identity is immutable';
    END IF;
    IF OLD.delivery_status IS NOT NULL
       AND NEW.delivery_status IS DISTINCT FROM OLD.delivery_status THEN
        RAISE EXCEPTION 'run delivery status is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER protect_run_revision_identity
BEFORE UPDATE ON control_plane.runs
FOR EACH ROW EXECUTE FUNCTION control_plane.protect_run_revision_identity();

CREATE FUNCTION control_plane.require_trusted_release_manifest()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.delivery_status = 'release_ready' AND NOT EXISTS (
        SELECT 1
        FROM control_plane.artifact_manifests AS manifest
        WHERE manifest.tenant_id = NEW.tenant_id
          AND manifest.run_id = NEW.run_id
          AND manifest.delivery_status = 'release_ready'
          AND manifest.trusted
    ) THEN
        RAISE EXCEPTION 'release_ready requires a trusted artifact manifest';
    END IF;
    RETURN NEW;
END;
$$;

CREATE CONSTRAINT TRIGGER require_trusted_release_manifest
AFTER INSERT OR UPDATE OF delivery_status ON control_plane.runs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION control_plane.require_trusted_release_manifest();
