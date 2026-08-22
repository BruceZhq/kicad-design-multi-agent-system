CREATE TABLE control_plane.conversation_deletions (
    tenant_id uuid NOT NULL,
    project_id uuid NOT NULL,
    thread_id varchar(200) NOT NULL CHECK (thread_id ~ '^[A-Za-z0-9._:-]{1,200}$'),
    deleted_by_issuer varchar(2048) NOT NULL,
    deleted_by_subject varchar(255) NOT NULL,
    deleted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (
        tenant_id, project_id, thread_id,
        deleted_by_issuer, deleted_by_subject
    ),
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES control_plane.projects (tenant_id, project_id)
        ON DELETE CASCADE
);

GRANT SELECT, INSERT ON control_plane.conversation_deletions TO ratsnest_app;

ALTER TABLE control_plane.conversation_deletions ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.conversation_deletions FORCE ROW LEVEL SECURITY;

CREATE POLICY conversation_deletions_tenant_isolation
    ON control_plane.conversation_deletions
    FOR ALL
    TO ratsnest_app
    USING (
        tenant_id = control_plane.current_tenant_id()
        AND deleted_by_issuer = control_plane.current_principal_issuer()
        AND deleted_by_subject = control_plane.current_principal_subject()
    )
    WITH CHECK (
        tenant_id = control_plane.current_tenant_id()
        AND deleted_by_issuer = control_plane.current_principal_issuer()
        AND deleted_by_subject = control_plane.current_principal_subject()
    );
