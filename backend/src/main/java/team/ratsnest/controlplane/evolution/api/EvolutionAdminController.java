package team.ratsnest.controlplane.evolution.api;

import static team.ratsnest.controlplane.shared.web.ApiHeaders.ORGANIZATION_HEADER;

import java.util.UUID;

import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;
import team.ratsnest.controlplane.evolution.application.EvolutionCandidateService;
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

    record EvaluateRequest(
            @Positive long expectedVersion,
            @Valid PatchPlanInput patchPlan,
            @Valid PatchBundleInput patchBundle) {
    }

    record ApproveRequest(
            @Positive long expectedVersion,
            @NotNull UUID trialId,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String reportDigest,
            @NotBlank @Size(max = 2000) String reason) {
    }
}
