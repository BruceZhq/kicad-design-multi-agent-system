package team.ratsnest.controlplane.run.domain.port;

import java.util.List;
import java.util.Optional;
import java.util.UUID;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.run.domain.model.ConversationSummary;
import team.ratsnest.controlplane.run.domain.model.DeliveryStatus;
import team.ratsnest.controlplane.run.domain.model.Run;

/** Persistence boundary for run aggregate state. */
public interface RunStore {

    void insert(Run run, AuthenticatedActor actor);

    Optional<Run> find(UUID tenantId, UUID runId);

    Optional<Run> findForUpdate(UUID tenantId, UUID runId);

    Optional<Run> findByIdempotency(UUID tenantId, UUID projectId, String key);

    int nextRevisionNumber(UUID tenantId, UUID rootRunId);

    Optional<Run> findLatestRevision(UUID tenantId, UUID rootRunId);

    List<Run> findRevisionChainThrough(UUID tenantId, UUID rootRunId, int revisionNumber);

    Optional<Run> findLatestForThread(UUID tenantId, UUID projectId, String threadId);

    boolean isConversationRemoved(
            UUID tenantId, UUID projectId, String threadId, AuthenticatedActor actor);

    void removeConversation(
            UUID tenantId, UUID projectId, String threadId, AuthenticatedActor actor);

    List<ConversationSummary> listConversations(UUID tenantId, UUID projectId, int limit);

    void setDeliveryStatus(UUID tenantId, UUID runId, DeliveryStatus status);

    boolean updateFromRuntime(UUID tenantId, UUID runId, RuntimeRun runtime);

    boolean markWaitingForInput(UUID tenantId, UUID runId);

    boolean markFailed(UUID tenantId, UUID runId, String code, String error);

    List<ReconciliationClaim> claimForReconciliation(String workerId, int batchSize);

    boolean releaseReconciliation(
            ReconciliationClaim claim, String workerId, int delaySeconds);

    record ReconciliationClaim(UUID tenantId, UUID runId, int attempts) {
    }
}
