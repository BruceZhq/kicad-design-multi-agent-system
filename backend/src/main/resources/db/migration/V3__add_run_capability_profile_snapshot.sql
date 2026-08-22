ALTER TABLE control_plane.runs
    ADD COLUMN profile_id varchar(64),
    ADD COLUMN profile_version varchar(32),
    ADD COLUMN profile_digest char(64),
    ADD CONSTRAINT runs_profile_snapshot_complete CHECK (
        (
            profile_id IS NULL
            AND profile_version IS NULL
            AND profile_digest IS NULL
        )
        OR (
            profile_id IS NOT NULL
            AND profile_version IS NOT NULL
            AND profile_digest IS NOT NULL
            AND profile_id ~ '^[a-z0-9][a-z0-9-]{1,63}$'
            AND profile_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))?$'
            AND profile_digest ~ '^[0-9a-f]{64}$'
        )
    );
