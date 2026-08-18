\set ON_ERROR_STOP on

-- Local/Compose bootstrap only. Production roles are provisioned by the
-- platform and the two passwords must be different secret values.
SELECT format(
    'CREATE ROLE ratsnest_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ratsnest_app')
\gexec

SELECT format(
    'ALTER ROLE ratsnest_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'app_password'
)
\gexec

SELECT format(
    'CREATE ROLE ratsnest_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'migrator_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ratsnest_migrator')
\gexec

SELECT format(
    'ALTER ROLE ratsnest_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD %L',
    :'migrator_password'
)
\gexec

SELECT 'CREATE SCHEMA control_plane AUTHORIZATION ratsnest_migrator'
WHERE NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'control_plane')
\gexec

ALTER SCHEMA control_plane OWNER TO ratsnest_migrator;
REVOKE ALL ON SCHEMA control_plane FROM PUBLIC;

-- Do not silently reassign objects from an older Compose volume. Ownership is
-- a security boundary and a broad REASSIGN OWNED can affect unrelated schemas.
-- Stop before Flyway and require an operator-reviewed, backed-up migration.
DO $bootstrap$
DECLARE
    offenders text;
BEGIN
    SELECT string_agg(kind || ' ' || object_name || ' (owner=' || owner_name || ')', ', ')
    INTO offenders
    FROM (
        SELECT 'relation' AS kind,
               quote_ident(c.relname) AS object_name,
               pg_get_userbyid(c.relowner) AS owner_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'control_plane'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
          AND pg_get_userbyid(c.relowner) <> 'ratsnest_migrator'
        UNION ALL
        SELECT 'routine',
               quote_ident(p.proname) || '(' || pg_get_function_identity_arguments(p.oid) || ')',
               pg_get_userbyid(p.proowner)
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'control_plane'
          AND pg_get_userbyid(p.proowner) <> 'ratsnest_migrator'
        UNION ALL
        SELECT 'type', quote_ident(t.typname), pg_get_userbyid(t.typowner)
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'control_plane'
          AND t.typrelid = 0
          AND t.typelem = 0
          AND pg_get_userbyid(t.typowner) <> 'ratsnest_migrator'
        ORDER BY 1, 2
        LIMIT 20
    ) AS unsafe_objects;

    IF offenders IS NOT NULL THEN
        RAISE EXCEPTION 'control_plane contains objects not owned by ratsnest_migrator: %', offenders
            USING HINT = 'Back up the database and use reviewed per-object ALTER ... OWNER statements; do not run automatic REASSIGN OWNED.';
    END IF;
END
$bootstrap$;
