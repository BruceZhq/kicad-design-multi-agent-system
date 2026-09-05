package team.ratsnest.controlplane.evolution.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;

import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository.CanaryArtifactEvidence;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository.CanaryMetricsEvidence;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.application.TenantAccess;
import team.ratsnest.controlplane.tenancy.domain.model.MembershipRole;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

class EvolutionCandidateServiceTest {

    private static final String DIGEST = "a".repeat(64);
    private static final AuthenticatedActor ACTOR =
            new AuthenticatedActor("https://issuer.example", "tenant-admin");

    @ParameterizedTest
    @EnumSource(
            value = EvolutionCandidate.Status.class,
            names = {"EVALUATING", "AWAITING_APPROVAL", "APPROVED", "CANARY", "PROMOTED"})
    void managementApiCannotManufactureEvaluationProof(EvolutionCandidate.Status target) {
        UUID tenantId = UUID.randomUUID();
        TenantAccess tenantAccess = mock(TenantAccess.class);
        EvolutionRepository repository = mock(EvolutionRepository.class);
        EvolutionCandidate candidate = candidate(EvolutionCandidate.Status.EVALUATING, 5);
        when(tenantAccess.requireMembership(tenantId, ACTOR)).thenReturn(MembershipRole.ADMIN);
        when(repository.findCandidate(tenantId, candidate.candidateId()))
                .thenReturn(Optional.of(candidate));
        EvolutionCandidateService service = service(tenantAccess, repository);

        ApiException failure = assertThrows(
                ApiException.class,
                () -> service.transition(
                        tenantId,
                        candidate.candidateId(),
                        candidate.rowVersion(),
                        target,
                        "manual approval",
                        ACTOR));

        assertThat(failure.code()).isEqualTo("EVOLUTION_EVALUATION_PROOF_REQUIRED");
        verify(repository, never()).transition(
                tenantId, candidate, target, "manual approval", ACTOR);
    }

    @Test
    void platformApprovalRequiresAndConsumesTheExactPassingTrialProof() {
        UUID tenantId = UUID.randomUUID();
        UUID trialId = UUID.randomUUID();
        TenantAccess tenantAccess = mock(TenantAccess.class);
        EvolutionRepository repository = mock(EvolutionRepository.class);
        EvolutionCandidate candidate = candidate(EvolutionCandidate.Status.AWAITING_APPROVAL, 7);
        EvolutionCandidate approved = candidate(EvolutionCandidate.Status.APPROVED, 8);
        Instant completedAt = Instant.parse("2026-08-19T00:05:00Z");
        EvolutionTrial trial = new EvolutionTrial(
                trialId,
                candidate.candidateId(),
                1,
                DIGEST,
                DIGEST,
                DIGEST,
                DIGEST,
                "ratsnest-evolution-" + trialId,
                null,
                DIGEST,
                null,
                DIGEST,
                DIGEST,
                DIGEST,
                Map.of(),
                Map.of(),
                Map.of(
                        "runtimeAttested", true,
                        "guardrailPassed", true,
                        "authoritativeGatePassed", true),
                "PASSED",
                DIGEST,
                Map.of("verdict", "passed"),
                null,
                0,
                100,
                2,
                completedAt.minusSeconds(60),
                completedAt,
                completedAt);
        when(repository.findCandidate(tenantId, candidate.candidateId()))
                .thenReturn(Optional.of(candidate), Optional.of(approved));
        when(repository.findTrial(tenantId, trialId)).thenReturn(Optional.of(trial));
        when(repository.transition(
                tenantId,
                candidate,
                EvolutionCandidate.Status.APPROVED,
                "reviewed proof",
                ACTOR)).thenReturn(true);

        EvolutionCandidate result = service(tenantAccess, repository).approveCandidate(
                tenantId,
                candidate.candidateId(),
                candidate.rowVersion(),
                trialId,
                DIGEST,
                "reviewed proof",
                ACTOR);

        assertThat(result.status()).isEqualTo(EvolutionCandidate.Status.APPROVED);
        verify(repository).transition(
                tenantId,
                candidate,
                EvolutionCandidate.Status.APPROVED,
                "reviewed proof",
                ACTOR);
    }

    @Test
    void canaryFailsClosedUntilARealHarnessRolloutIsBound() {
        UUID tenantId = UUID.randomUUID();
        UUID trialId = UUID.randomUUID();
        TenantAccess tenantAccess = mock(TenantAccess.class);
        EvolutionRepository repository = mock(EvolutionRepository.class);
        EvolutionCandidate approved = candidate(EvolutionCandidate.Status.APPROVED, 8);
        EvolutionTrial trial = passingTrial(trialId, approved.candidateId(), false);
        var evidence = new EvolutionCandidateService.CanaryArtifactInput(
                trialId,
                DIGEST,
                "b".repeat(40),
                DIGEST,
                "sha256:" + "c".repeat(64),
                "d".repeat(64),
                "evolution/artifacts/manifest.json",
                "e".repeat(64));
        when(repository.findCandidate(tenantId, approved.candidateId()))
                .thenReturn(Optional.of(approved));
        when(repository.findTrial(tenantId, trialId)).thenReturn(Optional.of(trial));

        ApiException failure = assertThrows(ApiException.class, () ->
                service(tenantAccess, repository).enterCanary(
                        tenantId, approved.candidateId(), approved.rowVersion(),
                        evidence, "start canary", ACTOR));

        assertThat(failure.code()).isEqualTo("EVOLUTION_CANARY_ROLLOUT_NOT_BOUND");
        verify(repository, never()).bindCanaryArtifacts(
                org.mockito.ArgumentMatchers.any(),
                org.mockito.ArgumentMatchers.any(),
                org.mockito.ArgumentMatchers.any());
    }

    @Test
    void promotionRejectsCanaryMetricsWithoutTheBoundDigest() {
        UUID tenantId = UUID.randomUUID();
        UUID trialId = UUID.randomUUID();
        TenantAccess tenantAccess = mock(TenantAccess.class);
        EvolutionRepository repository = mock(EvolutionRepository.class);
        EvolutionCandidate canary = candidate(EvolutionCandidate.Status.CANARY, 9);
        EvolutionTrial trial = passingTrial(trialId, canary.candidateId(), true);
        when(repository.findCandidate(tenantId, canary.candidateId()))
                .thenReturn(Optional.of(canary));
        when(repository.findTrial(tenantId, trialId)).thenReturn(Optional.of(trial));
        Instant start = trial.completedAt().plusSeconds(60);
        var evidence = new EvolutionCandidateService.CanaryMetricsInput(
                trialId, DIGEST, start, start.plusSeconds(3600), 20,
                1.0, 1.0, 0, 0, 0, 1.0, 1.0, 1.0,
                0, 0, 0, "f".repeat(64), DIGEST);

        ApiException failure = assertThrows(
                ApiException.class,
                () -> service(tenantAccess, repository).promoteCandidate(
                        tenantId,
                        canary.candidateId(),
                        canary.rowVersion(),
                        evidence,
                        "promote",
                        ACTOR));

        assertThat(failure.code()).isEqualTo("EVOLUTION_PROMOTION_TRUSTED_EVIDENCE_REQUIRED");
        verify(repository, never()).bindCanaryMetrics(
                org.mockito.ArgumentMatchers.any(),
                org.mockito.ArgumentMatchers.any(),
                org.mockito.ArgumentMatchers.any());
    }

    @Test
    void promotionNeverTrustsClientSuppliedPassingMetrics() throws Exception {
        UUID tenantId = UUID.randomUUID();
        UUID trialId = UUID.randomUUID();
        TenantAccess tenantAccess = mock(TenantAccess.class);
        EvolutionRepository repository = mock(EvolutionRepository.class);
        EvolutionCandidate canary = candidate(EvolutionCandidate.Status.CANARY, 9);
        EvolutionTrial trial = passingTrial(trialId, canary.candidateId(), true);
        Instant start = trial.completedAt().plusSeconds(60);
        Instant end = start.plusSeconds(3600);
        String sourceDigest = "f".repeat(64);
        String metricsDigest = canaryMetricsDigest(
                canary.candidateId(), trialId, start, end, 20,
                1.0, 1.0, 0, 0, 0, 1.0, 1.0, 1.0,
                0, 0, 0, sourceDigest);
        var evidence = new EvolutionCandidateService.CanaryMetricsInput(
                trialId, DIGEST, start, end, 20,
                1.0, 1.0, 0, 0, 0, 1.0, 1.0, 1.0,
                0, 0, 0, sourceDigest, metricsDigest);
        when(repository.findCandidate(tenantId, canary.candidateId()))
                .thenReturn(Optional.of(canary));
        when(repository.findTrial(tenantId, trialId)).thenReturn(Optional.of(trial));

        ApiException failure = assertThrows(ApiException.class, () ->
                service(tenantAccess, repository).promoteCandidate(
                        tenantId, canary.candidateId(), canary.rowVersion(),
                        evidence, "promote", ACTOR));

        assertThat(failure.code()).isEqualTo("EVOLUTION_PROMOTION_TRUSTED_EVIDENCE_REQUIRED");
        verify(repository, never()).bindCanaryMetrics(
                org.mockito.ArgumentMatchers.any(),
                org.mockito.ArgumentMatchers.any(),
                org.mockito.ArgumentMatchers.any());
    }

    private EvolutionCandidateService service(
            TenantAccess tenantAccess,
            EvolutionRepository repository) {
        return new EvolutionCandidateService(
                tenantAccess,
                mock(TenantContext.class),
                repository,
                new EvolutionRolloutService(
                        mock(team.ratsnest.controlplane.harness.application.HarnessVersionService.class),
                        mock(team.ratsnest.controlplane.harness.domain.port.HarnessVersionRepository.class),
                        mock(team.ratsnest.controlplane.evolution.domain.port.CanaryEvidenceStore.class),
                        new tools.jackson.databind.ObjectMapper(),
                        new team.ratsnest.controlplane.agentgateway.application.RuntimeVersionRoutes(
                                new tools.jackson.databind.ObjectMapper(), "{}"), "production", 10, 5));
    }

    private EvolutionCandidate candidate(EvolutionCandidate.Status status, long rowVersion) {
        Instant now = Instant.parse("2026-08-19T00:00:00Z");
        return new EvolutionCandidate(
                DIGEST,
                "harness-v1",
                DIGEST,
                "missing_grounded_symbol",
                "selection",
                "symbol_grounding",
                "grounding",
                "official_library_lookup",
                List.of("site-control-telemetry@1.0"),
                List.of("b".repeat(64)),
                3,
                2,
                "medium",
                "tool_adapter",
                status,
                null,
                rowVersion,
                now,
                now);
    }

    private EvolutionTrial passingTrial(UUID trialId, String candidateId, boolean canaryBound) {
        Instant completedAt = Instant.parse("2026-08-19T00:05:00Z");
        Map<String, Object> guardrails = canaryBound
                ? Map.of(
                        "runtimeAttested", true,
                        "guardrailPassed", true,
                        "authoritativeGatePassed", true,
                        "canaryArtifactsBound", true,
                        "artifactManifestDigest", "d".repeat(64),
                        "buildProvenanceDigest", "e".repeat(64))
                : Map.of(
                        "runtimeAttested", true,
                        "guardrailPassed", true,
                        "authoritativeGatePassed", true);
        return new EvolutionTrial(
                trialId,
                candidateId,
                1,
                DIGEST,
                DIGEST,
                DIGEST,
                DIGEST,
                "ratsnest-evolution-" + trialId,
                canaryBound ? "b".repeat(40) : null,
                DIGEST,
                canaryBound ? "sha256:" + "c".repeat(64) : null,
                DIGEST,
                DIGEST,
                DIGEST,
                Map.of(),
                Map.of(),
                guardrails,
                "PASSED",
                DIGEST,
                Map.of("verdict", "passed"),
                canaryBound ? "evolution/artifacts/manifest.json" : null,
                0,
                100,
                2,
                completedAt.minusSeconds(60),
                completedAt,
                completedAt);
    }

    private String canaryMetricsDigest(
            String candidateId,
            UUID trialId,
            Instant start,
            Instant end,
            int sampleSize,
            double releaseReadyRate,
            double strictReleaseEvidenceRate,
            int ercErrorCount,
            int drcErrorCount,
            int unconnectedCount,
            double routingCompletionRate,
            double coreArtifactClosureRate,
            double artifactIdentityRate,
            int falseReleaseCount,
            int regressionCount,
            int infrastructureFailureCount,
            String sourceDigest) throws Exception {
        String canonical = String.join("\0",
                "ratsnest-canary-metrics-v1",
                candidateId,
                trialId.toString(),
                DIGEST,
                start.toString(),
                end.toString(),
                Integer.toString(sampleSize),
                Double.toString(releaseReadyRate),
                Double.toString(strictReleaseEvidenceRate),
                Integer.toString(ercErrorCount),
                Integer.toString(drcErrorCount),
                Integer.toString(unconnectedCount),
                Double.toString(routingCompletionRate),
                Double.toString(coreArtifactClosureRate),
                Double.toString(artifactIdentityRate),
                Integer.toString(falseReleaseCount),
                Integer.toString(regressionCount),
                Integer.toString(infrastructureFailureCount),
                sourceDigest);
        return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256")
                .digest(canonical.getBytes(StandardCharsets.UTF_8)));
    }
}
