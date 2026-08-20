package team.ratsnest.controlplane.evolution.domain.model;

import java.time.Instant;
import java.util.List;
import java.util.Set;

public record EvolutionCandidate(
        String candidateId,
        String baseHarnessVersionId,
        String baseManifestDigest,
        String failureSignature,
        String step,
        String checkName,
        String category,
        String requiredCapability,
        List<String> profileReferences,
        List<String> observationIds,
        int occurrenceCount,
        int projectCount,
        String riskTier,
        String changeKind,
        Status status,
        String transitionReason,
        long rowVersion,
        Instant createdAt,
        Instant updatedAt) {

    public enum Status {
        OBSERVED,
        ELIGIBLE,
        EVALUATING,
        AWAITING_APPROVAL,
        APPROVED,
        CANARY,
        PROMOTED,
        REJECTED,
        ROLLED_BACK,
        STALE;

        public String wireValue() {
            return name().toLowerCase(java.util.Locale.ROOT);
        }

        public static Status fromWireValue(String value) {
            return Status.valueOf(value.toUpperCase(java.util.Locale.ROOT));
        }

        public boolean canTransitionTo(Status target) {
            return switch (this) {
                case OBSERVED -> Set.of(ELIGIBLE, REJECTED, STALE).contains(target);
                case ELIGIBLE -> Set.of(EVALUATING, REJECTED, STALE).contains(target);
                case EVALUATING -> Set.of(AWAITING_APPROVAL, REJECTED, STALE).contains(target);
                case AWAITING_APPROVAL -> Set.of(APPROVED, REJECTED, STALE).contains(target);
                case APPROVED -> Set.of(CANARY, STALE).contains(target);
                case CANARY -> Set.of(PROMOTED, ROLLED_BACK).contains(target);
                case PROMOTED -> target == ROLLED_BACK;
                case REJECTED, ROLLED_BACK, STALE -> false;
            };
        }
    }
}
