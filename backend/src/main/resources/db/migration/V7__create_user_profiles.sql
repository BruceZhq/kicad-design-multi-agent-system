CREATE TABLE control_plane.user_profiles (
    issuer varchar(2048) NOT NULL,
    subject varchar(255) NOT NULL,
    display_name varchar(120) NOT NULL CHECK (btrim(display_name) <> ''),
    job_title varchar(120) NOT NULL DEFAULT '',
    bio varchar(1000) NOT NULL DEFAULT '',
    locale varchar(35) NOT NULL DEFAULT 'zh-CN',
    time_zone varchar(64) NOT NULL DEFAULT 'Asia/Shanghai',
    avatar_object_key varchar(1024),
    avatar_media_type varchar(32),
    avatar_sha256 char(64),
    avatar_size_bytes bigint,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (issuer, subject),
    CHECK (
        (avatar_object_key IS NULL
         AND avatar_media_type IS NULL
         AND avatar_sha256 IS NULL
         AND avatar_size_bytes IS NULL)
        OR
        (avatar_object_key IS NOT NULL
         AND btrim(avatar_object_key) <> ''
         AND avatar_media_type IN ('image/jpeg', 'image/png', 'image/webp')
         AND avatar_sha256 ~ '^[0-9a-f]{64}$'
         AND avatar_size_bytes BETWEEN 1 AND 2097152)
    )
);

GRANT SELECT, INSERT, UPDATE ON control_plane.user_profiles TO ratsnest_app;

ALTER TABLE control_plane.user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE control_plane.user_profiles FORCE ROW LEVEL SECURITY;

CREATE POLICY user_profiles_principal_isolation
    ON control_plane.user_profiles
    FOR ALL
    TO ratsnest_app
    USING (
        issuer = control_plane.current_principal_issuer()
        AND subject = control_plane.current_principal_subject()
    )
    WITH CHECK (
        issuer = control_plane.current_principal_issuer()
        AND subject = control_plane.current_principal_subject()
    );
