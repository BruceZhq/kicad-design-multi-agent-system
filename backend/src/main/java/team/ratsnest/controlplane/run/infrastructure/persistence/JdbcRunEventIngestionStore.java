package team.ratsnest.controlplane.run.infrastructure.persistence;

import java.util.List;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;

import team.ratsnest.controlplane.run.domain.port.RunEventIngestionStore;

@Repository
public class JdbcRunEventIngestionStore implements RunEventIngestionStore {

    private final JdbcClient jdbcClient;

    public JdbcRunEventIngestionStore(JdbcClient jdbcClient) {
        this.jdbcClient = jdbcClient;
    }

    @Override
    public void recordObservedHighWater(
            java.util.UUID tenantId,
            java.util.UUID runId,
            long eventSequence) {
        int updated = jdbcClient.sql("""
                        update control_plane.runs
                        set newest_event_id = greatest(
                            coalesce(newest_event_id, 0), :eventSequence)
                        where tenant_id = :tenantId
                          and run_id = :runId
                        """)
                .param("eventSequence", eventSequence)
                .param("tenantId", tenantId)
                .param("runId", runId)
                .update();
        if (updated != 1) {
            throw new IllegalStateException(
                    "Run disappeared while recording the runtime event high-water");
        }
    }

    @Override
    public List<IngestionClaim> claim(String workerId, int batchSize) {
        return jdbcClient.sql(
                        "select * from control_plane.claim_run_event_ingestion(:workerId, :batchSize)")
                .param("workerId", workerId)
                .param("batchSize", batchSize)
                .query((resultSet, rowNumber) -> new IngestionClaim(
                        resultSet.getObject("tenant_id", java.util.UUID.class),
                        resultSet.getObject("run_id", java.util.UUID.class),
                        resultSet.getLong("last_event_seq"),
                        resultSet.getInt("ingest_attempts")))
                .list();
    }

    @Override
    public boolean advance(
            IngestionClaim claim,
            String workerId,
            long expectedEventSequence,
            long nextEventSequence) {
        return jdbcClient.sql("""
                        update control_plane.run_event_ingestion
                        set last_event_seq = :nextEventSequence,
                            updated_at = clock_timestamp()
                        where tenant_id = :tenantId
                          and run_id = :runId
                          and ingest_locked_by = :workerId
                          and last_event_seq = :expectedEventSequence
                          and :nextEventSequence > :expectedEventSequence
                        """)
                .param("nextEventSequence", nextEventSequence)
                .param("tenantId", claim.tenantId())
                .param("runId", claim.runId())
                .param("workerId", workerId)
                .param("expectedEventSequence", expectedEventSequence)
                .update() == 1;
    }

    @Override
    public boolean release(IngestionClaim claim, String workerId, int delaySeconds) {
        return Boolean.TRUE.equals(jdbcClient.sql("""
                        select control_plane.release_run_event_ingestion(
                            :tenantId, :runId, :workerId, :delaySeconds)
                        """)
                .param("tenantId", claim.tenantId())
                .param("runId", claim.runId())
                .param("workerId", workerId)
                .param("delaySeconds", delaySeconds)
                .query(Boolean.class)
                .single());
    }
}
