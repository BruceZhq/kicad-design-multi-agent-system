package team.ratsnest.controlplane.evolution;

import java.time.Instant;
import java.util.Map;
import java.util.UUID;

public record EvolutionTrial(
        UUID trialId,
        String candidateId,
        int attempt,
        String inputDigest,
        String baseManifestDigest,
        String candidateDigest,
        String evalSuiteDigest,
        String temporalWorkflowId,
        String patchCommit,
        String patchSha256,
        String candidateImageDigest,
        String optimizationSuiteDigest,
        String holdoutSuiteDigest,
        String adversarialSuiteDigest,
        Map<String, Object> baselineMetrics,
        Map<String, Object> candidateMetrics,
        Map<String, Object> guardrailResults,
        String verdict,
        String reportDigest,
        Map<String, Object> authoritativeReport,
        String reportObjectKey,
        long llmTokens,
        long wallClockMs,
        long rowVersion,
        Instant createdAt,
        Instant updatedAt,
        Instant completedAt) {
}
