package team.ratsnest.controlplane.run.domain.port;

import java.util.List;
import java.util.UUID;

/** Durable, control-plane-owned cursor and lease for runtime event ingestion. */
public interface RunEventIngestionStore {

    void recordObservedHighWater(UUID tenantId, UUID runId, long eventSequence);

    List<IngestionClaim> claim(String workerId, int batchSize);

    boolean advance(
            IngestionClaim claim,
            String workerId,
            long expectedEventSequence,
            long nextEventSequence);

    boolean release(IngestionClaim claim, String workerId, int delaySeconds);

    record IngestionClaim(
            UUID tenantId,
            UUID runId,
            long lastEventSequence,
            int attempts) {
    }
}
