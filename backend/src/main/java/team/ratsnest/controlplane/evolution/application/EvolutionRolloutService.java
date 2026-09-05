package team.ratsnest.controlplane.evolution.application;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.evolution.domain.port.CanaryEvidenceStore;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository;
import team.ratsnest.controlplane.harness.application.HarnessVersionService;
import team.ratsnest.controlplane.harness.domain.model.HarnessVersion;
import team.ratsnest.controlplane.harness.domain.port.HarnessVersionRepository;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import tools.jackson.databind.ObjectMapper;

/** Binds approved trials to existing, attested release infrastructure. */
@Service
public class EvolutionRolloutService {
    private final HarnessVersionService harness;
    private final HarnessVersionRepository versions;
    private final CanaryEvidenceStore evidence;
    private final ObjectMapper json;
    private final team.ratsnest.controlplane.agentgateway.application.RuntimeVersionRoutes routes;
    private final String rolloutId;
    private final int percent;
    private final int minimumSamples;

    public EvolutionRolloutService(HarnessVersionService harness, HarnessVersionRepository versions,
            CanaryEvidenceStore evidence, ObjectMapper json,
            team.ratsnest.controlplane.agentgateway.application.RuntimeVersionRoutes routes,
            @Value("${ratsnest.harness.rollout-id:production}") String rolloutId,
            @Value("${ratsnest.evolution.canary-percent:10}") int percent,
            @Value("${ratsnest.evolution.canary-minimum-samples:5}") int minimumSamples) {
        if (percent < 1 || percent > 25 || minimumSamples < 3) {
            throw new IllegalArgumentException("Governed rollout needs 1-25 percent and at least 3 independent tasks");
        }
        this.harness = harness; this.versions = versions; this.evidence = evidence;
        this.json = json; this.rolloutId = rolloutId; this.percent = percent;
        this.routes = routes;
        this.minimumSamples = minimumSamples;
    }

    public EvolutionRepository.CanaryArtifactEvidence activate(EvolutionCandidate candidate,
            EvolutionTrial trial, EvolutionCandidateService.CanaryArtifactInput input, AuthenticatedActor actor) {
        HarnessVersion version = versions.findByManifest(input.artifactManifestDigest())
                .orElseThrow(() -> denied("EVOLUTION_CANARY_ROLLOUT_NOT_BOUND"));
        routes.requireVersion(version.harnessVersionId());
        routes.requireVersion(candidate.baseHarnessVersionId()); // retain old runs and rollback target
        var base = harness.require(candidate.baseHarnessVersionId());
        var rollout = harness.rollout(rolloutId);
        if (!version.attested() || version.dirty()
                || !Objects.equals(version.parentVersionId(), candidate.baseHarnessVersionId())
                || !Objects.equals(rollout.stableVersionId(), candidate.baseHarnessVersionId())
                || rollout.canaryVersionId() != null
                || !Objects.equals(version.bundleDigest(), trial.patchSha256())
                || !Objects.equals(input.patchSha256(), trial.patchSha256())
                || !Objects.equals(version.sourceCommit(), input.patchCommit())
                || !Objects.equals(version.runtimeImageDigest(), input.candidateImageDigest())
                || !Objects.equals(version.manifestObjectKey(), input.artifactObjectKey())
                || !Objects.equals(version.toolchainDigest(), base.toolchainDigest())
                || !Objects.equals(version.contractDigest(), base.contractDigest())
                || !Objects.equals(version.policyDigest(), base.policyDigest())) {
            throw denied("EVOLUTION_CANARY_EVIDENCE_INVALID");
        }
        // Registration is a separate platform/CI authority. Never fabricate a
        // version or copy an image digest supplied to this approval endpoint.
        String provenance = digest(Map.of("sourceCommit", version.sourceCommit(),
                "sourceTreeDigest", version.sourceTreeDigest(), "patchSha256", trial.patchSha256(),
                "imageDigest", version.runtimeImageDigest(), "manifestDigest", version.manifestDigest()));
        if (!provenance.equals(input.buildProvenanceDigest())) {
            throw denied("EVOLUTION_CANARY_BUILD_PROVENANCE_MISMATCH");
        }
        if (version.releaseStatus() == HarnessVersion.ReleaseStatus.CANDIDATE) {
            harness.transition(version.harnessVersionId(), version.rowVersion(),
                    HarnessVersion.ReleaseStatus.APPROVED, "approved evolution trial " + trial.trialId(), actor);
        } else if (version.releaseStatus() != HarnessVersion.ReleaseStatus.APPROVED) {
            throw denied("EVOLUTION_CANARY_EVIDENCE_INVALID");
        }
        harness.configureCanary(rolloutId, rollout.rowVersion(), version.harnessVersionId(), percent, actor);
        return new EvolutionRepository.CanaryArtifactEvidence(version.sourceCommit(), trial.patchSha256(),
                version.runtimeImageDigest(), version.manifestDigest(), version.manifestObjectKey(), provenance,
                version.harnessVersionId(), rolloutId, Instant.now());
    }

    public Map<String, Object> report(UUID tenantId, EvolutionTrial trial) {
        var guards = trial.guardrailResults();
        if (!Boolean.TRUE.equals(guards.get("canaryArtifactsBound"))
                || !(guards.get("canaryHarnessVersionId") instanceof String versionId)
                || !(guards.get("canaryStartedAt") instanceof String start)
                || !rolloutId.equals(guards.get("canaryRolloutId"))) {
            throw denied("EVOLUTION_PROMOTION_TRUSTED_EVIDENCE_REQUIRED");
        }
        var version = harness.require(versionId);
        var rollout = harness.rollout(rolloutId);
        if (!versionId.equals(rollout.canaryVersionId()) || rollout.canaryPercent() <= 0
                || !version.attested() || !Objects.equals(version.runtimeImageDigest(), trial.candidateImageDigest())
                || !Objects.equals(version.manifestDigest(), guards.get("artifactManifestDigest"))) {
            throw denied("EVOLUTION_PROMOTION_TRUSTED_EVIDENCE_REQUIRED");
        }
        List<Map<String, Object>> rows = evidence.observations(tenantId, versionId, version.manifestDigest(), Instant.parse(start));
        long independent = rows.stream().map(r -> r.get("rootRunId")).distinct().count();
        boolean closed = !rows.isEmpty() && rows.size() <= 500 && rows.stream().allMatch(this::strictDelivery);
        Map<String, Object> result = new LinkedHashMap<>();
        result.put("harnessVersionId", versionId); result.put("manifestDigest", version.manifestDigest());
        result.put("since", start); result.put("independentTasks", independent); result.put("sampleSize", rows.size());
        result.put("minimumSamples", minimumSamples); result.put("observations", rows);
        result.put("eligible", independent >= minimumSamples && closed);
        result.put("source", "server_persisted_run_and_verified_artifact_manifests");
        result.put("metricsDigest", digest(result));
        return Map.copyOf(result);
    }

    public EvolutionRepository.CanaryMetricsEvidence promote(UUID tenantId, EvolutionTrial trial, AuthenticatedActor actor) {
        Map<String, Object> report = report(tenantId, trial);
        if (!Boolean.TRUE.equals(report.get("eligible"))) {
            throw denied("EVOLUTION_CANARY_NOT_RELEASE_READY");
        }
        var rollout = harness.rollout(rolloutId);
        harness.promote(rolloutId, rollout.rowVersion(), (String) report.get("harnessVersionId"), actor);
        return new EvolutionRepository.CanaryMetricsEvidence(report, (String) report.get("metricsDigest"));
    }

    private boolean strictDelivery(Map<String, Object> row) {
        String files = (String) row.get("files");
        return "COMPLETED".equals(row.get("state")) && Boolean.TRUE.equals(row.get("trusted"))
                && "release_ready".equals(row.get("deliveryStatus"))
                && List.of(".kicad_sch", ".kicad_pcb", ".dsn", ".ses", ".erc.json", ".drc.json")
                    .stream().allMatch(suffix -> java.util.Arrays.stream(files.split("\\|"))
                        .anyMatch(name -> name.endsWith(suffix)));
    }

    private String digest(Map<String, Object> value) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(
                    json.writeValueAsString(canonical(value)).getBytes(StandardCharsets.UTF_8)));
        } catch (Exception exception) { throw new IllegalStateException("Cannot digest canary evidence", exception); }
    }

    private Object canonical(Object value) {
        if (value instanceof Map<?, ?> map) {
            var sorted = new TreeMap<String, Object>();
            map.forEach((k, v) -> sorted.put(k.toString(), canonical(v))); return sorted;
        }
        if (value instanceof List<?> list) { return list.stream().map(this::canonical).toList(); }
        return value;
    }

    private ApiException denied(String code) {
        return new ApiException(code, HttpStatus.CONFLICT,
                "A matching attested build, active rollout and sufficient server-derived release evidence are required.");
    }
}
