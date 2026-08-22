package team.ratsnest.controlplane.run.domain.port;

import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.run.domain.model.RunInteraction;

/** Persistence boundary for durable human-in-the-loop interactions. */
public interface RunInteractionStore {

    boolean register(Run run, String interactionId, long stateVersion, Map<String, Object> request);

    Optional<RunInteraction> findForUpdate(UUID tenantId, UUID runId, String interactionId);

    boolean beginResponse(
            RunInteraction interaction,
            String idempotencyKey,
            String fingerprint,
            UUID responseRequestId,
            String answer,
            AuthenticatedActor actor);

    void markResponded(RunInteraction interaction);
}
