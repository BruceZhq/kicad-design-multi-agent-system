package team.ratsnest.controlplane.agentgateway.domain.port;

import java.util.UUID;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;

public interface RuntimeCredentials {

    String principalId(UUID tenantId, UUID projectId, AuthenticatedActor actor);

    String token(String method, String path, byte[] body, RuntimeIdentity identity, String runId);

    RuntimeClaims verifyRuntimeToken(
            String token,
            String method,
            String path,
            byte[] body,
            String expectedRunId);

    boolean verifyEvolutionResultAttestation(
            byte[] canonicalPayload,
            String payloadSha256,
            String signature);

    record RuntimeClaims(
            String subject,
            String tenantId,
            String projectId,
            String runId) {
    }
}
