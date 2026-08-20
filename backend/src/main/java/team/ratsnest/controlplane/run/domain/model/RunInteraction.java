package team.ratsnest.controlplane.run.domain.model;

import java.util.Map;
import java.util.UUID;

public record RunInteraction(
        UUID tenantId,
        String interactionId,
        UUID runId,
        String kind,
        long stateVersion,
        Map<String, Object> request,
        Status status,
        String responseIdempotencyKey,
        String responseFingerprint,
        UUID responseRequestId,
        String answer) {

    public enum Status {
        PENDING,
        RESPONDING,
        RESPONDED
    }
}
