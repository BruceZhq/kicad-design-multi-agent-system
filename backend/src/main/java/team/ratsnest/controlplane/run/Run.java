package team.ratsnest.controlplane.run;

import java.time.Instant;
import java.util.Map;
import java.util.UUID;

import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.agentgateway.InternalTaskSigner;
import team.ratsnest.controlplane.identity.AuthenticatedActor;

public record Run(
        UUID tenantId,
        UUID runId,
        UUID projectId,
        UUID rootRunId,
        UUID parentRunId,
        int revisionNumber,
        String threadId,
        String idempotencyKey,
        String requestFingerprint,
        String message,
        String model,
        Map<String, Object> runtimeConfig,
        String profileId,
        String profileVersion,
        String profileDigest,
        String runtimePrincipalId,
        String createdByIssuer,
        String createdBySubject,
        RunState state,
        DeliveryStatus deliveryStatus,
        String runtimeRunId,
        long eventCount,
        Long oldestEventId,
        Long newestEventId,
        String errorCode,
        String error,
        Instant createdAt,
        Instant startedAt,
        Instant finishedAt) {

    public Run(
            UUID tenantId,
            UUID runId,
            UUID projectId,
            String threadId,
            String idempotencyKey,
            String requestFingerprint,
            String message,
            String model,
            Map<String, Object> runtimeConfig,
            String profileId,
            String profileVersion,
            String profileDigest,
            String runtimePrincipalId,
            String createdByIssuer,
            String createdBySubject,
            RunState state,
            String runtimeRunId,
            long eventCount,
            Long oldestEventId,
            Long newestEventId,
            String errorCode,
            String error,
            Instant createdAt,
            Instant startedAt,
            Instant finishedAt) {
        this(
                tenantId, runId, projectId, runId, null, 1,
                threadId, idempotencyKey, requestFingerprint, message, model,
                runtimeConfig, profileId, profileVersion, profileDigest,
                runtimePrincipalId, createdByIssuer, createdBySubject, state, null,
                runtimeRunId, eventCount, oldestEventId, newestEventId,
                errorCode, error, createdAt, startedAt, finishedAt);
    }

    RuntimeIdentity runtimeIdentity(InternalTaskSigner signer) {
        String principalId = runtimePrincipalId;
        if (principalId == null || principalId.isBlank()) {
            principalId = signer.principalId(
                    tenantId,
                    projectId,
                    new AuthenticatedActor(createdByIssuer, createdBySubject));
        }
        return new RuntimeIdentity(
                principalId,
                tenantId.toString(),
                projectId.toString());
    }
}
