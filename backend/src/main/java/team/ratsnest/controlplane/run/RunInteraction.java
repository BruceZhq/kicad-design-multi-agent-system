package team.ratsnest.controlplane.run;

import java.util.Map;
import java.util.UUID;

record RunInteraction(
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

    enum Status {
        PENDING,
        RESPONDING,
        RESPONDED
    }
}
