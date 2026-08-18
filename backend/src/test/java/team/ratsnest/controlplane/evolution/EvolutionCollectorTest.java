package team.ratsnest.controlplane.evolution;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoMoreInteractions;
import static org.mockito.Mockito.when;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeMessage;
import team.ratsnest.controlplane.run.Run;
import team.ratsnest.controlplane.tenancy.TenantContext;
import tools.jackson.databind.ObjectMapper;

class EvolutionCollectorTest {

    private static final String DIGEST = "a".repeat(64);

    @Test
    void persistsOnlyPrivacySafeDerivedObservationAndDeduplicatesAtRepositoryBoundary() {
        EvolutionRepository repository = mock(EvolutionRepository.class);
        TenantContext tenants = mock(TenantContext.class);
        EvolutionCollector collector = new EvolutionCollector(
                repository,
                tenants,
                new ObjectMapper(),
                "evolution-test-fingerprint-secret-32-bytes");
        Run run = run();
        Map<String, Object> event = Map.of(
                "kind", "ahe_event",
                "event", "capability_gap",
                "step", "pipeline:selection",
                "revision", 2,
                "gap", Map.of(
                        "signature", "missing_symbol",
                        "category", "grounding",
                        "message", "customer requirement must never be stored"));
        RuntimeEvent runtimeEvent = new RuntimeEvent(
                7L,
                "message",
                new RuntimeMessage("custom", "", List.of(), null, null, Map.of(), event),
                null,
                null,
                Map.of());
        when(repository.insertObservation(eq(run.tenantId()), any())).thenReturn(false);

        collector.collect(run, runtimeEvent);

        ArgumentCaptor<EvolutionObservation> captured =
                ArgumentCaptor.forClass(EvolutionObservation.class);
        verify(repository).insertObservation(eq(run.tenantId()), captured.capture());
        EvolutionObservation observation = captured.getValue();
        assertThat(observation.failureSignature()).isEqualTo("missing_symbol");
        assertThat(observation.evidenceDigest()).matches("[0-9a-f]{64}");
        assertThat(observation.toString()).doesNotContain("customer requirement");
        verify(tenants).activate(run.tenantId());
        verifyNoMoreInteractions(repository);
    }

    private Run run() {
        UUID tenantId = UUID.randomUUID();
        UUID runId = UUID.randomUUID();
        return new Run(
                tenantId,
                runId,
                UUID.randomUUID(),
                "thread",
                "idempotency",
                DIGEST,
                "private requirement",
                "model",
                Map.of("harness_version", Map.of("manifest_digest", DIGEST)),
                "site-control-telemetry",
                "1.0",
                DIGEST,
                "harness-v1",
                DIGEST,
                "stable",
                "principal",
                "issuer",
                "subject",
                RunState.RUNNING,
                null,
                0,
                null,
                null,
                null,
                null,
                Instant.now(),
                Instant.now(),
                null);
    }
}
