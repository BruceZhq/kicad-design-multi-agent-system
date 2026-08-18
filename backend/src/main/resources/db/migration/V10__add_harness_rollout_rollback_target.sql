-- A promotion records its attested predecessor so a later rollback cannot
-- select an arbitrary retired release. The value is cleared after one use.
ALTER TABLE control_plane.harness_rollouts
    ADD COLUMN previous_stable_version_id varchar(120),
    ADD CONSTRAINT harness_rollouts_previous_stable_fk
        FOREIGN KEY (previous_stable_version_id)
        REFERENCES control_plane.harness_versions (harness_version_id),
    ADD CONSTRAINT harness_rollouts_previous_stable_distinct
        CHECK (
            previous_stable_version_id IS NULL
            OR previous_stable_version_id <> stable_version_id
        );

GRANT UPDATE (previous_stable_version_id)
    ON control_plane.harness_rollouts TO ratsnest_app;
