-- A replayed historical interaction may have the same Runtime ID in a new
-- revision Run. All reads and responses already require tenant_id + run_id.
-- Preserve old records while making the database key match that API boundary.
ALTER TABLE control_plane.run_interactions
    DROP CONSTRAINT run_interactions_pkey;
ALTER TABLE control_plane.run_interactions
    ADD PRIMARY KEY (tenant_id, run_id, interaction_id);
