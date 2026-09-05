package team.ratsnest.controlplane.evolution.api;

import static team.ratsnest.controlplane.shared.web.ApiHeaders.ORGANIZATION_HEADER;

import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.Map;

import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;
import team.ratsnest.controlplane.evolution.application.EvolutionCandidateService;
import team.ratsnest.controlplane.evolution.application.EvolutionCandidateService.CanaryArtifactInput;
import team.ratsnest.controlplane.evolution.application.EvolutionCandidateService.CanaryMetricsInput;
import team.ratsnest.controlplane.evolution.application.EvolutionTrialService.EvaluateCommand;
import team.ratsnest.controlplane.evolution.application.EvolutionTrialService.PatchBundleInput;
import team.ratsnest.controlplane.evolution.application.EvolutionTrialService.PatchPlanInput;
import team.ratsnest.controlplane.evolution.application.EvolutionTrialLauncher;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.identity.api.JwtPlatformPrincipal;
import team.ratsnest.controlplane.identity.application.PlatformAccess;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;

@RestController
@Validated
@RequestMapping("/api/v1/platform/evolution")
public class EvolutionAdminController {

    private final PlatformAccess platformAccess;
    private final EvolutionCandidateService candidates;
    private final EvolutionTrialLauncher trialLauncher;

    public EvolutionAdminController(
            PlatformAccess platformAccess,
            EvolutionCandidateService candidates,
            EvolutionTrialLauncher trialLauncher) {
        this.platformAccess = platformAccess;
        this.candidates = candidates;
        this.trialLauncher = trialLauncher;
    }

    @PostMapping("/candidates/{candidateId}:evaluate")
    EvolutionTrial evaluate(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @RequestHeader("Idempotency-Key")
                    @NotBlank @Pattern(regexp = "[A-Za-z0-9._:-]{8,200}") String idempotencyKey,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @Valid @RequestBody EvaluateRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return trialLauncher.evaluate(
                tenantId,
                candidateId,
                request.expectedVersion(),
                idempotencyKey,
                new EvaluateCommand(request.patchPlan(), request.patchBundle()),
                actor);
    }

    @PostMapping("/candidates/{candidateId}:propose-and-evaluate")
    EvolutionTrial proposeAndEvaluate(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @RequestHeader("Idempotency-Key")
                    @NotBlank @Pattern(regexp = "[A-Za-z0-9._:-]{8,200}") String idempotencyKey,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @Valid @RequestBody ProposeEvaluateRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return trialLauncher.proposeAndEvaluate(
                tenantId,
                candidateId,
                request.expectedVersion(),
                idempotencyKey,
                request.repositoryContextPaths(),
                actor);
    }

    @PostMapping("/candidates/{candidateId}:approve")
    EvolutionController.CandidateResponse approve(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @Valid @RequestBody ApproveRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return EvolutionController.CandidateResponse.from(candidates.approveCandidate(
                tenantId,
                candidateId,
                request.expectedVersion(),
                request.trialId(),
                request.reportDigest(),
                request.reason(),
                actor));
    }

    @PostMapping("/candidates/{candidateId}:canary")
    EvolutionController.CandidateResponse canary(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @Valid @RequestBody CanaryRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return EvolutionController.CandidateResponse.from(candidates.enterCanary(
                tenantId,
                candidateId,
                request.expectedVersion(),
                new CanaryArtifactInput(
                        request.trialId(),
                        request.reportDigest(),
                        request.patchCommit(),
                        request.patchSha256(),
                        request.candidateImageDigest(),
                        request.artifactManifestDigest(),
                        request.artifactObjectKey(),
                        request.buildProvenanceDigest()),
                request.reason(),
                actor));
    }

    @PostMapping("/candidates/{candidateId}:promote")
    EvolutionController.CandidateResponse promote(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @Valid @RequestBody PromoteRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return EvolutionController.CandidateResponse.from(candidates.promoteCandidate(
                tenantId,
                candidateId,
                request.expectedVersion(),
                new CanaryMetricsInput(
                        request.trialId(),
                        request.reportDigest(),
                        request.windowStartedAt(),
                        request.windowEndedAt(),
                        request.sampleSize(),
                        request.releaseReadyRate(),
                        request.strictReleaseEvidenceRate(),
                        request.ercErrorCount(),
                        request.drcErrorCount(),
                        request.unconnectedCount(),
                        request.routingCompletionRate(),
                        request.coreArtifactClosureRate(),
                        request.artifactIdentityRate(),
                        request.falseReleaseCount(),
                        request.regressionCount(),
                        request.infrastructureFailureCount(),
                        request.sourceDigest(),
                        request.metricsDigest()),
                request.reason(),
                actor));
    }

    @GetMapping("/candidates/{candidateId}/canary-report")
    Map<String, Object> canaryReport(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @RequestParam UUID trialId, @AuthenticationPrincipal Jwt jwt) {
        platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        return candidates.canaryReport(tenantId, candidateId, trialId);
    }

    @PostMapping("/candidates/{candidateId}:promote-verified")
    EvolutionController.CandidateResponse promoteVerified(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @Valid @RequestBody ApproveRequest request, @AuthenticationPrincipal Jwt jwt) {
        var actor = platformAccess.requireHarnessAdmin(JwtPlatformPrincipal.from(jwt));
        // Only identity/approval arrives from the caller; every metric is loaded by the server.
        return EvolutionController.CandidateResponse.from(candidates.promoteCandidate(
                tenantId, candidateId, request.expectedVersion(),
                new CanaryMetricsInput(request.trialId(), request.reportDigest(), null, null,
                        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, null, null), request.reason(), actor));
    }

    record EvaluateRequest(
            @Positive long expectedVersion,
            @Valid PatchPlanInput patchPlan,
            @Valid PatchBundleInput patchBundle) {
    }

    record ProposeEvaluateRequest(
            @Positive long expectedVersion,
            @Size(min = 1, max = 32) List<@NotBlank @Size(max = 500) String>
                    repositoryContextPaths) {
    }

    record ApproveRequest(
            @Positive long expectedVersion,
            @NotNull UUID trialId,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String reportDigest,
            @NotBlank @Size(max = 2000) String reason) {
    }

    record CanaryRequest(
            @Positive long expectedVersion,
            @NotNull UUID trialId,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String reportDigest,
            @NotBlank @Pattern(regexp = "[0-9a-f]{40,64}") String patchCommit,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String patchSha256,
            @NotBlank @Pattern(regexp = "sha256:[0-9a-f]{64}") String candidateImageDigest,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String artifactManifestDigest,
            @NotBlank @Size(max = 1024) String artifactObjectKey,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String buildProvenanceDigest,
            @NotBlank @Size(max = 2000) String reason) {
    }

    record PromoteRequest(
            @Positive long expectedVersion,
            @NotNull UUID trialId,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String reportDigest,
            @NotNull Instant windowStartedAt,
            @NotNull Instant windowEndedAt,
            @Positive int sampleSize,
            double releaseReadyRate,
            double strictReleaseEvidenceRate,
            @PositiveOrZero int ercErrorCount,
            @PositiveOrZero int drcErrorCount,
            @PositiveOrZero int unconnectedCount,
            double routingCompletionRate,
            double coreArtifactClosureRate,
            double artifactIdentityRate,
            @PositiveOrZero int falseReleaseCount,
            @PositiveOrZero int regressionCount,
            @PositiveOrZero int infrastructureFailureCount,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String sourceDigest,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String metricsDigest,
            @NotBlank @Size(max = 2000) String reason) {
    }
}
