package team.ratsnest.controlplane.tenancy;

import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(
        prefix = "ratsnest.database",
        name = "verify-isolation",
        havingValue = "true",
        matchIfMissing = true)
public class DatabaseIsolationVerifier implements ApplicationRunner {

    private final JdbcClient jdbcClient;

    public DatabaseIsolationVerifier(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    @Override
    public void run(ApplicationArguments arguments) {
        IsolationState state = jdbcClient.sql("""
                        select
                            current_user as role_name,
                            (select rolsuper from pg_roles where rolname = current_user) as superuser,
                            (select rolbypassrls from pg_roles where rolname = current_user) as bypass_rls,
                            pg_has_role(current_user, 'ratsnest_migrator', 'member') as migrator_member,
                            (select pg_get_userbyid(nspowner) = current_user
                             from pg_namespace where nspname = 'control_plane') as schema_owner,
                            (select count(*)
                             from pg_class join pg_namespace on pg_namespace.oid = relnamespace
                             where nspname = 'control_plane'
                               and relkind = 'r'
                               and relname <> 'flyway_schema_history') as business_tables,
                            (select count(*)
                             from pg_class join pg_namespace on pg_namespace.oid = relnamespace
                             where nspname = 'control_plane'
                               and relkind = 'r'
                               and relname <> 'flyway_schema_history'
                               and pg_get_userbyid(relowner) = current_user) as owned_tables,
                            (select count(*)
                             from pg_class join pg_namespace on pg_namespace.oid = relnamespace
                             where nspname = 'control_plane'
                               and relkind = 'r'
                               and relname <> 'flyway_schema_history'
                               -- Global release metadata has no tenant_id and is only
                               -- mutated through the platform-admin control plane.
                               and relname not in ('harness_versions', 'harness_rollouts')
                               and (
                                   not relrowsecurity
                                   or (
                                       not relforcerowsecurity
                                       and relname not in ('runs', 'run_outbox')
                                   )
                               )) as unprotected_tables
                        """)
                .query((resultSet, rowNumber) -> new IsolationState(
                        resultSet.getString("role_name"),
                        resultSet.getBoolean("superuser"),
                        resultSet.getBoolean("bypass_rls"),
                        resultSet.getBoolean("migrator_member"),
                        resultSet.getBoolean("schema_owner"),
                        resultSet.getLong("business_tables"),
                        resultSet.getLong("owned_tables"),
                        resultSet.getLong("unprotected_tables")))
                .single();

        if (!state.secure()) {
            throw new IllegalStateException("PostgreSQL runtime role or RLS configuration is unsafe");
        }
    }

    private record IsolationState(
            String roleName,
            boolean superuser,
            boolean bypassRls,
            boolean migratorMember,
            boolean schemaOwner,
            long businessTables,
            long ownedTables,
            long unprotectedTables) {

        boolean secure() {
            return "ratsnest_app".equals(roleName)
                    && !superuser
                    && !bypassRls
                    && !migratorMember
                    && !schemaOwner
                    && businessTables >= 3
                    && ownedTables == 0
                    && unprotectedTables == 0;
        }
    }
}
