package team.ratsnest.controlplane.evolution.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.verifyNoMoreInteractions;
import static org.mockito.Mockito.when;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeMessage;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservation;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservationGovernance;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;
import tools.jackson.databind.ObjectMapper;

class EvolutionCollectorTest {

    private static final String DIGEST = "a".repeat(64);
    private static final String RECORD_ID = "b".repeat(64);
    private static final String SIGNATURE = "systemic-signature";

    @Test
    void validatedRecordIdIsTheReplayStableObservationIdentity() {
        EvolutionRepository repository = mock(EvolutionRepository.class);
        TenantContext tenants = mock(TenantContext.class);
        EvolutionCollector collector = collector(repository, tenants);
        Run run = run();
        when(repository.insertObservation(eq(run.tenantId()), any(), any())).thenReturn(false);

        Map<String, Object> payload = governedEvent(
                "harness_defect_observed", "harness_observation", "observe_harness",
                "harness_defect_not_yet_cross_run_reproducible", 1, 1);
        collector.collect(run, runtimeEvent(7, payload));
        collector.collect(run, runtimeEvent(99, payload));

        ArgumentCaptor<EvolutionObservation> observations =
                ArgumentCaptor.forClass(EvolutionObservation.class);
        ArgumentCaptor<EvolutionObservationGovernance> governance =
                ArgumentCaptor.forClass(EvolutionObservationGovernance.class);
        verify(repository, org.mockito.Mockito.times(2)).insertObservation(
                eq(run.tenantId()), observations.capture(), governance.capture());
        assertThat(observations.getAllValues())
                .extracting(EvolutionObservation::observationId)
                .containsOnly(RECORD_ID);
        assertThat(observations.getAllValues())
                .extracting(EvolutionObservation::sourceEventSeq)
                .containsExactly(7L, 99L);
        assertThat(governance.getAllValues())
                .extracting(EvolutionObservationGovernance::attributionReasonCode)
                .containsOnly("harness_defect_not_yet_cross_run_reproducible");
        verify(tenants, org.mockito.Mockito.times(2)).activate(run.tenantId());
        verifyNoMoreInteractions(repository);
    }

    @Test
    void rejectsUntrustedOrUnderThresholdCapabilityGapAttribution() {
        EvolutionRepository repository = mock(EvolutionRepository.class);
        TenantContext tenants = mock(TenantContext.class);
        EvolutionCollector collector = collector(repository, tenants);
        Run run = run();
        List<Map<String, Object>> invalid = new ArrayList<>();
        invalid.add(governedEvent(
                "capability_gap", "capability_gap", "capability_gap",
                "cross_run_reproducible_harness_defect", 1, 2));
        invalid.add(governedEvent(
                "capability_gap", "capability_gap", "capability_gap",
                "cross_run_reproducible_harness_defect", 2, 1));
        invalid.add(governedEvent(
                "capability_gap", "harness_observation", "capability_gap",
                "cross_run_reproducible_harness_defect", 2, 2));
        invalid.add(governedEvent(
                "capability_gap", "capability_gap", "observe_harness",
                "cross_run_reproducible_harness_defect", 2, 2));
        invalid.add(governedEvent(
                "capability_gap", "capability_gap", "capability_gap",
                "ordinary_design_issue", 2, 2));
        Map<String, Object> designOrigin = governedEvent(
                "capability_gap", "capability_gap", "capability_gap",
                "cross_run_reproducible_harness_defect", 2, 2);
        @SuppressWarnings("unchecked")
        Map<String, Object> failure = new LinkedHashMap<>(
                (Map<String, Object>) designOrigin.get("failure"));
        failure.put("origin", "design");
        designOrigin.put("failure", failure);
        invalid.add(designOrigin);
        Map<String, Object> unknownFailureReason = governedEvent(
                "capability_gap", "capability_gap", "capability_gap",
                "cross_run_reproducible_harness_defect", 2, 2);
        @SuppressWarnings("unchecked")
        Map<String, Object> unknownReasonFailure = new LinkedHashMap<>(
                (Map<String, Object>) unknownFailureReason.get("failure"));
        unknownReasonFailure.put("reason_code", "arbitrary_model_claim");
        unknownFailureReason.put("failure", unknownReasonFailure);
        invalid.add(unknownFailureReason);
        Map<String, Object> missingFailureReason = governedEvent(
                "capability_gap", "capability_gap", "capability_gap",
                "cross_run_reproducible_harness_defect", 2, 2);
        @SuppressWarnings("unchecked")
        Map<String, Object> noReasonFailure = new LinkedHashMap<>(
                (Map<String, Object>) missingFailureReason.get("failure"));
        noReasonFailure.remove("reason_code");
        missingFailureReason.put("failure", noReasonFailure);
        invalid.add(missingFailureReason);

        long eventId = 1;
        for (Map<String, Object> candidate : invalid) {
            collector.collect(run, runtimeEvent(eventId++, candidate));
        }

        verifyNoInteractions(repository, tenants);
    }

    @Test
    void countsTrustedHarnessObservationsAndGapAcrossTwoProjectsWithoutOffByOne() {
        EvolutionRepository repository = mock(EvolutionRepository.class);
        TenantContext tenants = mock(TenantContext.class);
        EvolutionCollector collector = collector(repository, tenants);
        Run run = run();
        when(repository.insertObservation(eq(run.tenantId()), any(), any())).thenReturn(true);
        when(repository.findActiveGaps(
                run.tenantId(), run.harnessVersionId(), run.harnessManifestDigest(), SIGNATURE))
                .thenReturn(List.of(
                        observation("1".repeat(64), "harness_defect_observed", "project-a", "run-a"),
                        observation("2".repeat(64), "harness_defect_observed", "project-b", "run-b"),
                        observation("3".repeat(64), "capability_gap", "project-b", "run-b")));

        collector.collect(run, runtimeEvent(7, governedEvent(
                "capability_gap", "capability_gap", "capability_gap",
                "cross_run_reproducible_harness_defect", 2, 2)));

        ArgumentCaptor<EvolutionCandidate> candidate =
                ArgumentCaptor.forClass(EvolutionCandidate.class);
        verify(repository).upsertAggregate(eq(run.tenantId()), candidate.capture());
        assertThat(candidate.getValue().status()).isEqualTo(EvolutionCandidate.Status.ELIGIBLE);
        assertThat(candidate.getValue().projectCount()).isEqualTo(2);
        assertThat(candidate.getValue().occurrenceCount()).isEqualTo(3);
    }

    @Test
    void candidateIdentityPartitionsTheSameManifestAcrossHarnessVersions() {
        EvolutionRepository repository = mock(EvolutionRepository.class);
        TenantContext tenants = mock(TenantContext.class);
        EvolutionCollector collector = collector(repository, tenants);
        UUID tenantId = UUID.randomUUID();
        Run first = run(tenantId, "harness-v1");
        Run second = run(tenantId, "harness-v2");
        when(repository.insertObservation(eq(tenantId), any(), any())).thenReturn(true);
        when(repository.findActiveGaps(tenantId, "harness-v1", DIGEST, SIGNATURE))
                .thenReturn(List.of(observation(
                        "1".repeat(64), "capability_gap", "project-a", "run-a", "harness-v1")));
        when(repository.findActiveGaps(tenantId, "harness-v2", DIGEST, SIGNATURE))
                .thenReturn(List.of(observation(
                        "2".repeat(64), "capability_gap", "project-b", "run-b", "harness-v2")));

        Map<String, Object> firstPayload = governedEvent(
                "capability_gap", "capability_gap", "capability_gap",
                "cross_run_reproducible_harness_defect", 2, 2);
        Map<String, Object> secondPayload = governedEvent(
                "capability_gap", "capability_gap", "capability_gap",
                "cross_run_reproducible_harness_defect", 2, 2);
        secondPayload.put("record_id", "c".repeat(64));
        collector.collect(first, runtimeEvent(7, firstPayload));
        collector.collect(second, runtimeEvent(8, secondPayload));

        ArgumentCaptor<EvolutionCandidate> candidates =
                ArgumentCaptor.forClass(EvolutionCandidate.class);
        verify(repository, org.mockito.Mockito.times(2))
                .upsertAggregate(eq(tenantId), candidates.capture());
        assertThat(candidates.getAllValues())
                .extracting(EvolutionCandidate::baseHarnessVersionId)
                .containsExactly("harness-v1", "harness-v2");
        assertThat(candidates.getAllValues())
                .extracting(EvolutionCandidate::candidateId)
                .doesNotHaveDuplicates();
    }

    @Test
    void onlyStrictlyAttributedResolutionCanCloseTheCurrentProjectGap() {
        EvolutionRepository repository = mock(EvolutionRepository.class);
        TenantContext tenants = mock(TenantContext.class);
        EvolutionCollector collector = collector(repository, tenants);
        Run run = run();
        when(repository.insertObservation(eq(run.tenantId()), any(), any())).thenReturn(true);
        when(repository.findActiveGaps(
                run.tenantId(), run.harnessVersionId(), run.harnessManifestDigest(), SIGNATURE))
                .thenReturn(List.of());

        collector.collect(run, runtimeEvent(8, resolutionEvent()));

        ArgumentCaptor<EvolutionObservationGovernance> governance =
                ArgumentCaptor.forClass(EvolutionObservationGovernance.class);
        verify(repository).insertObservation(eq(run.tenantId()), any(), governance.capture());
        assertThat(governance.getValue().attributionAction())
                .isEqualTo("resolve_capability_gap");
        verify(repository).markAggregateStale(
                run.tenantId(), run.harnessVersionId(), run.harnessManifestDigest(), SIGNATURE);
    }

    @Test
    void rejectsPayloadFieldsThatThePrivacySafeRuntimeSchemaDoesNotAllow() {
        EvolutionRepository repository = mock(EvolutionRepository.class);
        TenantContext tenants = mock(TenantContext.class);
        EvolutionCollector collector = collector(repository, tenants);
        Map<String, Object> payload = governedEvent(
                "harness_defect_observed", "harness_observation", "observe_harness",
                "harness_defect_not_yet_cross_run_reproducible", 1, 1);
        @SuppressWarnings("unchecked")
        Map<String, Object> failure = new LinkedHashMap<>(
                (Map<String, Object>) payload.get("failure"));
        failure.put("message", "private customer prompt");
        payload.put("failure", failure);

        collector.collect(run(), runtimeEvent(7, payload));

        verifyNoInteractions(repository, tenants);
    }

    private EvolutionCollector collector(EvolutionRepository repository, TenantContext tenants) {
        return new EvolutionCollector(
                repository,
                tenants,
                new ObjectMapper(),
                "evolution-test-fingerprint-secret-32-bytes");
    }

    private Map<String, Object> governedEvent(
            String eventType,
            String recoverability,
            String action,
            String reason,
            int projects,
            int runs) {
        Map<String, Object> payload = baseRecord(eventType);
        payload.put("failure", Map.of(
                "failure_id", "failure-1",
                "signature", SIGNATURE,
                "step", "schematic_connections",
                "check_name", "step_execution_failed",
                "category", "unknown",
                "recoverability", recoverability,
                "required_capability", "schematic_connectivity_repair",
                "origin", "harness",
                "reason_code", "generic_capability_closure_contradiction"));
        payload.put("attribution", Map.of(
                "action", action,
                "reason_code", reason,
                "origin", "harness",
                "independent_project_count", projects,
                "independent_run_count", runs));
        return payload;
    }

    private Map<String, Object> resolutionEvent() {
        Map<String, Object> payload = baseRecord("capability_gap_resolved");
        payload.put("gap", Map.of(
                "gap_id", "gap:" + SIGNATURE,
                "signature", SIGNATURE,
                "step", "schematic_connections",
                "check_name", "step_execution_failed",
                "category", "unknown",
                "required_capability", "schematic_connectivity_repair",
                "status", "observed"));
        payload.put("attribution", Map.of(
                "action", "resolve_capability_gap",
                "reason_code", "verified_harness_capability_gap_resolved",
                "origin", "harness",
                "independent_project_count", 1,
                "independent_run_count", 1));
        return payload;
    }

    private Map<String, Object> baseRecord(String eventType) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("kind", "ahe_event");
        payload.put("event", eventType);
        payload.put("step", "schematic_connections");
        payload.put("revision", 2);
        payload.put("schema_version", 1);
        payload.put("record_id", RECORD_ID);
        payload.put("created_at", "2026-08-20T12:00:00+00:00");
        return payload;
    }

    private RuntimeEvent runtimeEvent(long eventId, Map<String, Object> payload) {
        return new RuntimeEvent(
                eventId,
                "message",
                new RuntimeMessage("custom", "", List.of(), null, null, Map.of(), payload),
                null,
                null,
                Map.of());
    }

    private EvolutionObservation observation(
            String id,
            String eventType,
            String projectFingerprint,
            String scopeFingerprint) {
        return observation(id, eventType, projectFingerprint, scopeFingerprint, "harness-v1");
    }

    private EvolutionObservation observation(
            String id,
            String eventType,
            String projectFingerprint,
            String scopeFingerprint,
            String harnessVersionId) {
        Instant now = Instant.parse("2026-08-20T12:00:00Z");
        return new EvolutionObservation(
                id, UUID.randomUUID(), 1, harnessVersionId, "stable", DIGEST,
                "site-control-telemetry@1.0", DIGEST, scopeFingerprint, projectFingerprint,
                eventType, SIGNATURE, "schematic_connections", "step_execution_failed",
                "unknown", eventType.equals("capability_gap")
                        ? "capability_gap" : "harness_observation",
                null, "schematic_connectivity_repair", "observed", 2, DIGEST, now, now);
    }

    private Run run() {
        return run(UUID.randomUUID(), "harness-v1");
    }

    private Run run(UUID tenantId, String harnessVersionId) {
        UUID runId = UUID.randomUUID();
        return new Run(
                tenantId, runId, UUID.randomUUID(), "thread", "idempotency", DIGEST,
                "private requirement", "model",
                Map.of("harness_version", Map.of("manifest_digest", DIGEST)),
                "site-control-telemetry", "1.0", DIGEST,
                harnessVersionId, DIGEST, "stable", "principal", "issuer", "subject",
                RunState.RUNNING, null, 0, null, null, null, null,
                Instant.now(), Instant.now(), null);
    }
}
