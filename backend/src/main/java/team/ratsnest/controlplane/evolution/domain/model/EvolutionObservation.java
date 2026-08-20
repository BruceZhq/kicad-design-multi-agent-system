package team.ratsnest.controlplane.evolution.domain.model;

import java.time.Instant;
import java.util.UUID;

public record EvolutionObservation(
        String observationId,
        UUID runId,
        long sourceEventSeq,
        String harnessVersionId,
        String harnessChannel,
        String harnessManifestDigest,
        String profileReference,
        String profileDigest,
        String scopeFingerprint,
        String projectFingerprint,
        String eventType,
        String failureSignature,
        String step,
        String checkName,
        String category,
        String recoverability,
        String strategy,
        String requiredCapability,
        String outcome,
        int revision,
        String evidenceDigest,
        Instant observedAt,
        Instant recordedAt) {
}
