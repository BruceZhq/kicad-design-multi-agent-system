-- stateVersion is an optimistic-concurrency token of an interaction, not a
-- globally unique sequence for a Run. Different graph interrupts can refer
-- to the same engineering revision (e.g. two different requests at version 5).
-- V20 already gives each request the correct (tenant, run, interaction) key.
-- Keep that key and immutable request validation; do not rewrite old events,
-- accepted responses, engineering checkpoints, or Redis response deduplication.
ALTER TABLE control_plane.run_interactions
    DROP CONSTRAINT run_interactions_tenant_id_run_id_interaction_version_key;

CREATE INDEX run_interactions_latest_idx
    ON control_plane.run_interactions (tenant_id, run_id, created_at DESC);
