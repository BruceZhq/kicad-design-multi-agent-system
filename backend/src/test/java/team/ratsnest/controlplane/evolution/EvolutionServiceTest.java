package team.ratsnest.controlplane.evolution;

import static org.assertj.core.api.Assertions.assertThat;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.UUID;

import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.EnumSource;

import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.MembershipRole;
import team.ratsnest.controlplane.tenancy.TenantAccess;

class EvolutionServiceTest {

    private static final String DIGEST = "a".repeat(64);
    private static final AuthenticatedActor ACTOR =
            new AuthenticatedActor("https://issuer.example", "tenant-admin");

    @ParameterizedTest
    @EnumSource(
            value = EvolutionCandidate.Status.class,
            names = {"AWAITING_APPROVAL", "APPROVED"})
    void managementApiCannotManufactureEvaluationProof(EvolutionCandidate.Status target) {
        UUID tenantId = UUID.randomUUID();
        TenantAccess tenantAccess = mock(TenantAccess.class);
        EvolutionRepository repository = mock(EvolutionRepository.class);
        EvolutionCandidate candidate = candidate();
        when(tenantAccess.requireMembership(tenantId, ACTOR)).thenReturn(MembershipRole.ADMIN);
        when(repository.findCandidate(tenantId, candidate.candidateId()))
                .thenReturn(Optional.of(candidate));
        EvolutionService service = new EvolutionService(tenantAccess, repository);

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

    private EvolutionCandidate candidate() {
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
                EvolutionCandidate.Status.EVALUATING,
                null,
                5,
                now,
                now);
    }
}
