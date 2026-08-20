package team.ratsnest.controlplane.run.domain.model;

import java.time.Instant;
import java.util.Map;
import java.util.UUID;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.agentgateway.domain.port.RuntimeCredentials;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;

public record Run(
        UUID tenantId,
        UUID runId,
        UUID projectId,
        UUID rootRunId,
        UUID parentRunId,
        UUID forkedFromRunId,
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
        String harnessVersionId,
        String harnessManifestDigest,
        String harnessChannel,
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
            String harnessVersionId,
            String harnessManifestDigest,
            String harnessChannel,
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
        this(
                tenantId, runId, projectId, rootRunId, parentRunId, null, revisionNumber,
                threadId, idempotencyKey, requestFingerprint, message, model,
                runtimeConfig, profileId, profileVersion, profileDigest,
                harnessVersionId, harnessManifestDigest, harnessChannel,
                runtimePrincipalId, createdByIssuer, createdBySubject, state, deliveryStatus,
                runtimeRunId, eventCount, oldestEventId, newestEventId,
                errorCode, error, createdAt, startedAt, finishedAt);
    }

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
            String harnessVersionId,
            String harnessManifestDigest,
            String harnessChannel,
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
                tenantId, runId, projectId, runId, null, null, 1,
                threadId, idempotencyKey, requestFingerprint, message, model,
                runtimeConfig, profileId, profileVersion, profileDigest,
                harnessVersionId, harnessManifestDigest, harnessChannel,
                runtimePrincipalId, createdByIssuer, createdBySubject, state, null,
                runtimeRunId, eventCount, oldestEventId, newestEventId,
                errorCode, error, createdAt, startedAt, finishedAt);
    }

    public RuntimeIdentity runtimeIdentity(RuntimeCredentials signer) {
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
