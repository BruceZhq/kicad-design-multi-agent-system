package team.ratsnest.controlplane.bootstrap;

import java.util.Map;

import org.flywaydb.core.Flyway;
import org.flywaydb.core.api.output.MigrateResult;

/** Runs schema migrations without starting the control-plane application. */
public final class FlywayMigrationMain {

    private static final String SCHEMA = "control_plane";

    private FlywayMigrationMain() {
    }

    public static void main(String[] args) {
        Map<String, String> environment = System.getenv();
        MigrateResult result = Flyway.configure()
                .dataSource(
                        required(environment, "RATSNEST_FLYWAY_URL"),
                        required(environment, "RATSNEST_FLYWAY_USER"),
                        required(environment, "RATSNEST_FLYWAY_PASSWORD"))
                .schemas(SCHEMA)
                .defaultSchema(SCHEMA)
                .locations("classpath:db/migration")
                .cleanDisabled(true)
                .validateOnMigrate(true)
                .load()
                .migrate();

        System.out.printf(
                "Flyway migration completed: schema=%s migrationsExecuted=%d%n",
                SCHEMA,
                result.migrationsExecuted);
    }

    static String required(Map<String, String> environment, String name) {
        String value = environment.get(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException(name + " is required");
        }
        return value;
    }
}
