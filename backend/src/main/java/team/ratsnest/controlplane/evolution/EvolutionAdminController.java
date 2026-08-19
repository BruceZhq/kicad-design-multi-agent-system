package team.ratsnest.controlplane.evolution;

import static team.ratsnest.controlplane.organization.OrganizationController.ORGANIZATION_HEADER;

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
import team.ratsnest.controlplane.evolution.EvolutionService.EvaluateCommand;
import team.ratsnest.controlplane.evolution.EvolutionService.PatchBundleInput;
import team.ratsnest.controlplane.evolution.EvolutionService.PatchPlanInput;
import team.ratsnest.controlplane.evolution.EvolutionService.PreparedTrial;
import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.identity.PlatformAccess;

@RestController
@Validated
@RequestMapping("/api/v1/platform/evolution")
public class EvolutionAdminController {

    private final PlatformAccess platformAccess;
    private final EvolutionService evolution;
    private final EvolutionRuntimeGateway runtime;

    public EvolutionAdminController(
            PlatformAccess platformAccess,
            EvolutionService evolution,
            EvolutionRuntimeGateway runtime) {
        this.platformAccess = platformAccess;
        this.evolution = evolution;
        this.runtime = runtime;
    }

    @PostMapping("/candidates/{candidateId}:evaluate")
    EvolutionTrial evaluate(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @RequestHeader("Idempotency-Key")
                    @NotBlank @Pattern(regexp = "[A-Za-z0-9._:-]{8,200}") String idempotencyKey,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @Valid @RequestBody EvaluateRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(jwt);
        PreparedTrial prepared = evolution.prepareTrial(
                tenantId,
                candidateId,
                request.expectedVersion(),
                idempotencyKey,
                new EvaluateCommand(request.patchPlan(), request.patchBundle()),
                actor);
        if (!prepared.needsStart()) {
            return prepared.trial();
        }
        String workflowId = "ratsnest-evolution-" + prepared.trial().trialId();
        EvolutionTrial bound = evolution.bindWorkflow(
                tenantId, prepared.trial().trialId(), workflowId);
        EvolutionRuntimeGateway.StartResult started = runtime.start(
                tenantId, bound, prepared.trialInput());
        return evolution.bindWorkflow(tenantId, bound.trialId(), started.workflowId());
    }

    @PostMapping("/candidates/{candidateId}:approve")
    EvolutionController.CandidateResponse approve(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @Valid @RequestBody ApproveRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        AuthenticatedActor actor = platformAccess.requireHarnessAdmin(jwt);
        return EvolutionController.CandidateResponse.from(evolution.approveCandidate(
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
