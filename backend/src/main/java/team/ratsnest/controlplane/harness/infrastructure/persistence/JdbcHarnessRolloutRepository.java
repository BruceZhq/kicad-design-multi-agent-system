package team.ratsnest.controlplane.harness.infrastructure.persistence;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.Optional;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.harness.domain.model.HarnessRollout;
import team.ratsnest.controlplane.harness.domain.port.HarnessRolloutRepository;

@Repository
public class JdbcHarnessRolloutRepository implements HarnessRolloutRepository {

    private final JdbcClient jdbcClient;

    public JdbcHarnessRolloutRepository(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    @Override
    public Optional<HarnessRollout> find(String rolloutId) {
        return jdbcClient.sql("""
                        select rollout_id, stable_version_id, previous_stable_version_id,
                               canary_version_id, canary_percent, row_version,
                               updated_by, updated_at
                        from control_plane.harness_rollouts
                        where rollout_id = :rolloutId
                        """)
                .param("rolloutId", rolloutId)
                .query(JdbcHarnessRolloutRepository::map)
                .optional();
    }

    @Override
    public boolean configureCanary(
            HarnessRollout current,
            String canaryVersionId,
            int canaryPercent,
            String updatedBy) {
        return jdbcClient.sql("""
                        update control_plane.harness_rollouts
                        set canary_version_id = :canaryVersionId,
                            canary_percent = :canaryPercent,
                            updated_by = :updatedBy,
                            row_version = row_version + 1,
                            updated_at = now()
                        where rollout_id = :rolloutId
                          and row_version = :expectedVersion
                        """)
                .param("canaryVersionId", canaryVersionId)
                .param("canaryPercent", canaryPercent)
                .param("updatedBy", updatedBy)
                .param("rolloutId", current.rolloutId())
                .param("expectedVersion", current.rowVersion())
                .update() == 1;
    }

    @Override
    public boolean promote(
            HarnessRollout current,
            String promotedVersionId,
            String previousStableVersionId,
            String updatedBy) {
        return jdbcClient.sql("""
                        update control_plane.harness_rollouts
                        set stable_version_id = :promotedVersionId,
                            previous_stable_version_id = :previousStableVersionId,
                            canary_version_id = null,
                            canary_percent = 0,
                            updated_by = :updatedBy,
                            row_version = row_version + 1,
                            updated_at = now()
                        where rollout_id = :rolloutId
                          and row_version = :expectedVersion
                          and stable_version_id = :currentStableVersionId
                          and canary_version_id = :promotedVersionId
                        """)
                .param("promotedVersionId", promotedVersionId)
                .param("previousStableVersionId", previousStableVersionId)
                .param("updatedBy", updatedBy)
                .param("rolloutId", current.rolloutId())
                .param("expectedVersion", current.rowVersion())
                .param("currentStableVersionId", current.stableVersionId())
                .update() == 1;
    }

    @Override
    public boolean rollback(HarnessRollout current, String targetVersionId, String updatedBy) {
        return jdbcClient.sql("""
                        update control_plane.harness_rollouts
                        set stable_version_id = :targetVersionId,
                            previous_stable_version_id = null,
                            canary_version_id = null,
                            canary_percent = 0,
                            updated_by = :updatedBy,
                            row_version = row_version + 1,
                            updated_at = now()
                        where rollout_id = :rolloutId
                          and row_version = :expectedVersion
                          and stable_version_id = :currentStableVersionId
                          and previous_stable_version_id = :targetVersionId
                        """)
                .param("targetVersionId", targetVersionId)
                .param("updatedBy", updatedBy)
                .param("rolloutId", current.rolloutId())
                .param("expectedVersion", current.rowVersion())
                .param("currentStableVersionId", current.stableVersionId())
                .update() == 1;
    }

    private static HarnessRollout map(ResultSet resultSet, int rowNumber) throws SQLException {
        return new HarnessRollout(
                resultSet.getString("rollout_id"),
                resultSet.getString("stable_version_id"),
                resultSet.getString("previous_stable_version_id"),
                resultSet.getString("canary_version_id"),
                resultSet.getInt("canary_percent"),
                resultSet.getLong("row_version"),
                resultSet.getString("updated_by"),
                instant(resultSet, "updated_at"));
    }

    private static Instant instant(ResultSet resultSet, String column) throws SQLException {
        return resultSet.getObject(column, OffsetDateTime.class).toInstant();
    }
}
