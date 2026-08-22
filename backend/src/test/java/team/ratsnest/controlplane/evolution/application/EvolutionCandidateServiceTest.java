package team.ratsnest.controlplane.evolution.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Instant;
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
            names = {"EVALUATING", "AWAITING_APPROVAL", "APPROVED"})
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

    private EvolutionCandidateService service(
            TenantAccess tenantAccess,
            EvolutionRepository repository) {
        return new EvolutionCandidateService(
                tenantAccess,
                mock(TenantContext.class),
                repository);
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
}
