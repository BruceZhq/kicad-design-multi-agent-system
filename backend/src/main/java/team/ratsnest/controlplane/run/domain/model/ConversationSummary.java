package team.ratsnest.controlplane.run.domain.model;

import java.time.Instant;
import java.util.Map;
import java.util.UUID;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;

public record ConversationSummary(
        String threadId,
        String title,
        UUID latestRunId,
        int latestRevisionNumber,
        RunState state,
        DeliveryStatus deliveryStatus,
        long lastEventId,
        Map<String, Object> pendingInteraction,
        Instant createdAt,
        Instant updatedAt) {

    private static final int MAX_TITLE_LENGTH = 80;

    public static ConversationSummary fromStoredMessage(
            String threadId,
            String message,
            UUID latestRunId,
            int latestRevisionNumber,
            RunState state,
            DeliveryStatus deliveryStatus,
            long lastEventId,
            Map<String, Object> pendingInteraction,
            Instant createdAt,
            Instant updatedAt) {
        String normalized = message == null ? "" : message.replaceAll("\\s+", " ").strip();
        if (normalized.isEmpty()) {
            normalized = "未命名工程会话";
        } else if (normalized.length() > MAX_TITLE_LENGTH) {
            normalized = normalized.substring(0, MAX_TITLE_LENGTH).stripTrailing() + "…";
        }
        return new ConversationSummary(
                threadId,
                normalized,
                latestRunId,
                latestRevisionNumber,
                state,
                deliveryStatus,
                lastEventId,
                pendingInteraction,
                createdAt,
                updatedAt);
    }
}
