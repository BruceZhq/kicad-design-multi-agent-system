-- V15's trigger and claim/release functions execute as the schema-owning
-- migration role. FORCE ROW LEVEL SECURITY therefore requires an explicit
-- policy for that trusted role; the application policy remains tenant-scoped.
CREATE POLICY run_event_ingestion_migrator_internal
    ON control_plane.run_event_ingestion
    FOR ALL
    TO ratsnest_migrator
    USING (true)
    WITH CHECK (true);
