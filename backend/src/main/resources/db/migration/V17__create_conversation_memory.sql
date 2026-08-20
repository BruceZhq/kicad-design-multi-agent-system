CREATE TABLE control_plane.conversation_memories (
    memory_id uuid PRIMARY KEY,
    tenant_scope char(16) NOT NULL CHECK (tenant_scope ~ '^[0-9a-f]{16}$'),
    principal_scope char(16) NOT NULL CHECK (principal_scope ~ '^[0-9a-f]{16}$'),
    project_scope char(16) NOT NULL CHECK (project_scope ~ '^[0-9a-f]{16}$'),
    thread_id varchar(200) NOT NULL,
    request_id varchar(200) NOT NULL,
    memory_type varchar(32) NOT NULL CHECK (memory_type IN ('episodic', 'user_fact', 'outcome')),
    memory_key varchar(200) NOT NULL,
    summary text NOT NULL CHECK (length(summary) BETWEEN 1 AND 4000),
    value_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(384) NOT NULL,
    source_type varchar(32) NOT NULL CHECK (source_type IN ('user_statement', 'verified_outcome')),
    source_sha256 char(64) NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status varchar(16) NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'superseded', 'contested')),
    occurred_at timestamptz NOT NULL,
    last_accessed_at timestamptz,
    expires_at timestamptz,
    search_document tsvector GENERATED ALWAYS AS
        (to_tsvector('simple', coalesce(memory_key, '') || ' ' || coalesce(summary, ''))) STORED,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_scope, principal_scope, source_sha256)
);

CREATE INDEX conversation_memories_scope_time_idx
    ON control_plane.conversation_memories
    (tenant_scope, principal_scope, status, occurred_at DESC);
CREATE INDEX conversation_memories_key_idx
    ON control_plane.conversation_memories
    (tenant_scope, principal_scope, memory_key, status);
CREATE INDEX conversation_memories_search_idx
    ON control_plane.conversation_memories USING gin (search_document);
CREATE INDEX conversation_memories_embedding_idx
    ON control_plane.conversation_memories USING hnsw (embedding vector_cosine_ops);

REVOKE ALL ON control_plane.conversation_memories FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON control_plane.conversation_memories TO ratsnest_app;
