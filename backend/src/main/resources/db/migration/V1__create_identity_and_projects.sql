CREATE TABLE control_plane.organizations (
    tenant_id uuid PRIMARY KEY,
    name varchar(200) NOT NULL CHECK (btrim(name) <> ''),
    created_by_issuer varchar(2048) NOT NULL,
    created_by_subject varchar(255) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE control_plane.memberships (
    tenant_id uuid NOT NULL,
    issuer varchar(2048) NOT NULL,
    subject varchar(255) NOT NULL,
    membership_role varchar(16) NOT NULL
        CHECK (membership_role IN ('owner', 'admin', 'engineer', 'reviewer', 'viewer')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, issuer, subject),
    FOREIGN KEY (tenant_id)
        REFERENCES control_plane.organizations (tenant_id)
        ON DELETE CASCADE
);

CREATE INDEX memberships_principal_idx
    ON control_plane.memberships (issuer, subject, tenant_id);

CREATE TABLE control_plane.projects (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    name varchar(200) NOT NULL CHECK (btrim(name) <> ''),
    description varchar(2000) NOT NULL DEFAULT '',
    created_by_issuer varchar(2048) NOT NULL,
    created_by_subject varchar(255) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id),
    FOREIGN KEY (tenant_id)
        REFERENCES control_plane.organizations (tenant_id)
        ON DELETE CASCADE
);

CREATE INDEX projects_tenant_created_idx
    ON control_plane.projects (tenant_id, created_at DESC, project_id);

CREATE FUNCTION control_plane.current_tenant_id()
RETURNS uuid
LANGUAGE sql
STABLE
PARALLEL SAFE
RETURN NULLIF(current_setting('ratsnest.tenant_id', true), '')::uuid;

REVOKE ALL ON SCHEMA control_plane FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane.current_tenant_id() FROM PUBLIC;
GRANT USAGE ON SCHEMA control_plane TO ratsnest_app;
GRANT EXECUTE ON FUNCTION control_plane.current_tenant_id() TO ratsnest_app;
GRANT SELECT, INSERT ON control_plane.organizations TO ratsnest_app;
GRANT SELECT, INSERT, UPDATE ON control_plane.memberships TO ratsnest_app;
GRANT SELECT, INSERT, UPDATE ON control_plane.projects TO ratsnest_app;

ALTER TABLE control_plane.organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.organizations FORCE ROW LEVEL SECURITY;
CREATE POLICY organizations_tenant_isolation
    ON control_plane.organizations
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

ALTER TABLE control_plane.memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY memberships_tenant_isolation
    ON control_plane.memberships
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());

ALTER TABLE control_plane.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.projects FORCE ROW LEVEL SECURITY;
CREATE POLICY projects_tenant_isolation
    ON control_plane.projects
    FOR ALL
    TO ratsnest_app
    USING (tenant_id = control_plane.current_tenant_id())
    WITH CHECK (tenant_id = control_plane.current_tenant_id());
