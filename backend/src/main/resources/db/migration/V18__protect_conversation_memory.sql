-- Conversation memory belongs to the Agent Runtime, not the Java application role.
-- FORCE RLS makes accidental access by ratsnest_app fail closed. The runtime uses
-- its separately configured PostgreSQL identity and opaque tenant/principal scopes.
alter table control_plane.conversation_memories enable row level security;
alter table control_plane.conversation_memories force row level security;

revoke all on table control_plane.conversation_memories from ratsnest_app;
