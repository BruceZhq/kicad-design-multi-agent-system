package team.ratsnest.controlplane.evolution.domain.port;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservation;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservationGovernance;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;

/** Persistence port used by the governed-evolution application layer. */
public interface EvolutionRepository {

    List<EvolutionObservation> findObservations(UUID tenantId);

    boolean insertObservation(
            UUID tenantId,
            EvolutionObservation value,
            EvolutionObservationGovernance governance);

    List<EvolutionObservation> findActiveGaps(
            UUID tenantId,
            String harnessVersionId,
            String harnessManifestDigest,
            String failureSignature);

    void upsertAggregate(UUID tenantId, EvolutionCandidate candidate);

    void markAggregateStale(
            UUID tenantId,
            String harnessVersionId,
            String harnessManifestDigest,
            String failureSignature);

    List<EvolutionCandidate> findCandidates(UUID tenantId);

    Optional<EvolutionCandidate> findCandidate(UUID tenantId, String candidateId);

    List<EvolutionTrial> findTrials(UUID tenantId, String candidateId);

    Optional<EvolutionTrial> findTrial(UUID tenantId, UUID trialId);

    Optional<EvolutionTrial> findPendingTrial(UUID tenantId, String candidateId);

    int nextAttempt(UUID tenantId, String candidateId);

    boolean insertTrial(UUID tenantId, EvolutionTrial trial);

    boolean bindWorkflow(UUID tenantId, EvolutionTrial trial, String workflowId);

    boolean completeTrial(UUID tenantId, EvolutionTrial trial, TrialResult result);

    boolean transition(
            UUID tenantId,
            EvolutionCandidate candidate,
            EvolutionCandidate.Status target,
            String reason,
            AuthenticatedActor actor);

    record TrialResult(
            String temporalWorkflowId,
            String patchCommit,
            String patchSha256,
            String candidateImageDigest,
            Map<String, Object> baselineMetrics,
            Map<String, Object> candidateMetrics,
            Map<String, Object> guardrailResults,
            String verdict,
            String reportDigest,
            Map<String, Object> authoritativeReport,
            String reportObjectKey,
            long llmTokens,
            long wallClockMs,
            Instant completedAt) {
    }
}
