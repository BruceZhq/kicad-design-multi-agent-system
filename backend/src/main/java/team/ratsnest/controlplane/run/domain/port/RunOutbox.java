package team.ratsnest.controlplane.run.domain.port;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/** Transactional outbox boundary for run lifecycle and runtime events. */
public interface RunOutbox {

    boolean append(UUID tenantId, UUID runId, String eventType, Map<String, Object> payload);

    boolean appendSourceEvent(
            UUID tenantId,
            UUID runId,
            long sourceEventSeq,
            String eventType,
            Map<String, Object> payload);

    List<OutboxEvent> claim(String workerId, int batchSize);

    boolean acknowledge(UUID eventId, String workerId);

    boolean retry(UUID eventId, String workerId, int delaySeconds);

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
