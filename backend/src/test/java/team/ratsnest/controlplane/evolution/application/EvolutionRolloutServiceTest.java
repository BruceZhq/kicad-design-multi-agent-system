package team.ratsnest.controlplane.evolution.application;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.stream.IntStream;
import org.junit.jupiter.api.Test;
import team.ratsnest.controlplane.agentgateway.application.RuntimeVersionRoutes;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.evolution.domain.port.CanaryEvidenceStore;
import team.ratsnest.controlplane.harness.application.HarnessVersionService;
import team.ratsnest.controlplane.harness.domain.model.HarnessRollout;
import team.ratsnest.controlplane.harness.domain.model.HarnessVersion;
import team.ratsnest.controlplane.harness.domain.port.HarnessVersionRepository;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import tools.jackson.databind.ObjectMapper;

class EvolutionRolloutServiceTest {
    private final UUID tenant = UUID.randomUUID();
    private final HarnessVersionService harness = mock(HarnessVersionService.class);
    private final CanaryEvidenceStore store = mock(CanaryEvidenceStore.class);
    private final EvolutionTrial trial = mock(EvolutionTrial.class);
    private final AuthenticatedActor actor = new AuthenticatedActor("issuer", "admin");
    private final ObjectMapper mapper = new ObjectMapper();

    private EvolutionRolloutService service() {
        var version = mock(HarnessVersion.class);
        when(version.attested()).thenReturn(true);
        when(version.runtimeImageDigest()).thenReturn("sha256:" + "a".repeat(64));
        when(version.manifestDigest()).thenReturn("b".repeat(64));
        when(harness.require("new")).thenReturn(version);
        when(harness.rollout("production")).thenReturn(new HarnessRollout(
                "production", "old", null, "new", 10, 3, "admin", Instant.now()));
        var runtimeImageDigest = version.runtimeImageDigest();
        var manifestDigest = version.manifestDigest();
        when(trial.candidateImageDigest()).thenReturn(runtimeImageDigest);
        when(trial.guardrailResults()).thenReturn(Map.of(
                "canaryArtifactsBound", true, "canaryHarnessVersionId", "new",
                "canaryRolloutId", "production", "canaryStartedAt", "2026-09-05T00:00:00Z",
                "artifactManifestDigest", manifestDigest));
        return new EvolutionRolloutService(harness, mock(HarnessVersionRepository.class), store, mapper,
                new RuntimeVersionRoutes(mapper, "{}"), "production", 10, 5);
    }

    private List<Map<String, Object>> rows(boolean failed, boolean duplicateRoots) {
        return IntStream.range(0, 5).mapToObj(i -> Map.<String, Object>of(
                "runId", "run-" + i, "rootRunId", duplicateRoots ? "one-root" : "root-" + i,
                "state", "COMPLETED", "deliveryStatus", failed && i == 4 ? "execution_blocked" : "release_ready",
                "trusted", true, "manifestDigest", "d".repeat(64),
                "files", "board.kicad_sch|board.kicad_pcb|board.dsn|board.ses|board.erc.json|board.drc.json")).toList();
    }

    @Test void promotesActualRolloutWithServerEvidenceAndRetainsReport() {
        var service = service();
        when(store.observations(eq(tenant), eq("new"), any(), any())).thenReturn(rows(false, false));
        var report = service.promote(tenant, trial, actor);
        assertThat(report.metrics().get("eligible")).isEqualTo(true);
        assertThat(report.metrics().get("independentTasks")).isEqualTo(5L);
        assertThat(report.evidenceDigest()).hasSize(64);
        verify(harness).promote("production", 3, "new", actor);
    }

    @Test void rejectsAnyFailedCanaryInsteadOfSelectingOnlySuccessfulManifests() {
        var service = service();
        when(store.observations(eq(tenant), eq("new"), any(), any())).thenReturn(rows(true, false));
        assertThatThrownBy(() -> service.promote(tenant, trial, actor)).isInstanceOf(ApiException.class);
    }

    @Test void resumedRevisionsCannotInflateIndependentSampleSize() {
        var service = service();
        when(store.observations(eq(tenant), eq("new"), any(), any())).thenReturn(rows(false, true));
        assertThat(service.report(tenant, trial).get("eligible")).isEqualTo(false);
    }

    @Test void promotionDoesNotRetargetOldPinnedConversations() {
        var routes = new RuntimeVersionRoutes(mapper,
                "{\"old\":{\"http\":\"http://runtime-old:8080\"},\"new\":{\"http\":\"http://runtime-new:8080\"}}");
        assertThat(routes.endpoint("stable@new").get("http")).isEqualTo(routes.endpoint("canary@new").get("http"));
        assertThat(routes.endpoint("stable@old").get("http")).isEqualTo("http://runtime-old:8080");
    }
}
