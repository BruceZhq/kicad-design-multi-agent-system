package team.ratsnest.controlplane.run;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.springframework.jdbc.core.simple.JdbcClient;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

import tools.jackson.databind.ObjectMapper;

@Repository
class RunOutboxRepository {

    private final JdbcClient jdbcClient;
    private final ObjectMapper objectMapper;

    RunOutboxRepository(JdbcClient jdbcClient, ObjectMapper objectMapper) {
        this.jdbcClient = jdbcClient;
        this.objectMapper = objectMapper;
    }

    @Transactional(propagation = Propagation.MANDATORY)
    boolean append(UUID tenantId, UUID runId, String eventType, Map<String, Object> payload) {
        return append(tenantId, runId, null, eventType, payload);
    }

    @Transactional(propagation = Propagation.MANDATORY)
    boolean appendSourceEvent(
            UUID tenantId,
            UUID runId,
            long sourceEventSeq,
            String eventType,
            Map<String, Object> payload) {
        if (sourceEventSeq <= 0) {
            throw new IllegalArgumentException("Source event sequence must be positive");
        }
        return append(tenantId, runId, sourceEventSeq, eventType, payload);
    }

    private boolean append(
            UUID tenantId,
            UUID runId,
            Long sourceEventSeq,
            String eventType,
            Map<String, Object> payload) {
        return Boolean.TRUE.equals(jdbcClient.sql("""
                        select control_plane.append_run_outbox(
                            :tenantId, :eventId, :runId, :sourceEventSeq,
                            :eventType, cast(:payload as jsonb)
                        )
                        """)
                .param("tenantId", tenantId)
                .param("eventId", UUID.randomUUID())
                .param("runId", runId)
                .param("sourceEventSeq", sourceEventSeq)
                .param("eventType", eventType)
                .param("payload", json(payload))
                .query(Boolean.class)
                .single());
    }

    List<OutboxEvent> claim(String workerId, int batchSize) {
        return jdbcClient.sql("select * from control_plane.claim_run_outbox(:workerId, :batchSize)")
                .param("workerId", workerId)
                .param("batchSize", batchSize)
                .query(this::map)
                .list();
    }

    boolean acknowledge(UUID eventId, String workerId) {
        return Boolean.TRUE.equals(jdbcClient
                .sql("select control_plane.ack_run_outbox(:eventId, :workerId)")
                .param("eventId", eventId)
                .param("workerId", workerId)
                .query(Boolean.class)
                .single());
    }

    boolean retry(UUID eventId, String workerId, int delaySeconds) {
        return Boolean.TRUE.equals(jdbcClient
                .sql("select control_plane.retry_run_outbox(:eventId, :workerId, :delaySeconds)")
                .param("eventId", eventId)
                .param("workerId", workerId)
                .param("delaySeconds", delaySeconds)
                .query(Boolean.class)
                .single());
    }

    private OutboxEvent map(ResultSet resultSet, int rowNumber) throws SQLException {
        return new OutboxEvent(
                resultSet.getObject("tenant_id", UUID.class),
                resultSet.getObject("event_id", UUID.class),
                resultSet.getObject("run_id", UUID.class),
                resultSet.getLong("state_version"),
                nullableLong(resultSet, "source_event_seq"),
                resultSet.getString("event_type"),
                jsonObject(resultSet.getString("payload")),
                resultSet.getObject("occurred_at", OffsetDateTime.class).toInstant(),
                resultSet.getInt("publish_attempts"));
    }

    private String json(Map<String, Object> value) {
        try {
            return objectMapper.writeValueAsString(value);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to serialize run outbox payload", exception);
        }
    }

    private Long nullableLong(ResultSet resultSet, String column) throws SQLException {
        long value = resultSet.getLong(column);
        return resultSet.wasNull() ? null : value;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonObject(String value) {
        try {
            return (Map<String, Object>) objectMapper.readValue(value, Map.class);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to read run outbox payload", exception);
        }
    }

    record OutboxEvent(
            UUID tenantId,
            UUID eventId,
            UUID runId,
            long stateVersion,
            Long sourceEventSeq,
            String eventType,
            Map<String, Object> payload,
            Instant occurredAt,
            int publishAttempts) {
    }
}
