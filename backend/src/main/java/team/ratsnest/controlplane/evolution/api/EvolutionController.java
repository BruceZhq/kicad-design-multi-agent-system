package team.ratsnest.controlplane.evolution.api;

import static team.ratsnest.controlplane.shared.web.ApiHeaders.ORGANIZATION_HEADER;

import java.time.Instant;
import java.util.List;
import java.util.UUID;

import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;
import team.ratsnest.controlplane.evolution.application.EvolutionCandidateService;
import team.ratsnest.controlplane.evolution.application.EvolutionQueryService;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservation;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.identity.api.JwtIdentity;

@RestController
@Validated
@RequestMapping("/api/v1/evolution")
public class EvolutionController {

    private final EvolutionQueryService queries;
    private final EvolutionCandidateService candidates;

    public EvolutionController(
            EvolutionQueryService queries,
            EvolutionCandidateService candidates) {
        this.queries = queries;
        this.candidates = candidates;
    }

    @GetMapping("/observations")
    List<EvolutionObservation> observations(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @AuthenticationPrincipal Jwt jwt) {
        return queries.observations(tenantId, JwtIdentity.from(jwt));
    }

    @GetMapping("/candidates")
    List<CandidateResponse> candidates(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @AuthenticationPrincipal Jwt jwt) {
        return queries.candidates(tenantId, JwtIdentity.from(jwt)).stream()
                .map(CandidateResponse::from)
                .toList();
    }

    @GetMapping("/candidates/{candidateId}")
    CandidateResponse candidate(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @AuthenticationPrincipal Jwt jwt) {
        return CandidateResponse.from(
                queries.candidate(tenantId, candidateId, JwtIdentity.from(jwt)));
    }

    @GetMapping("/candidates/{candidateId}/trials")
    List<EvolutionTrial> trials(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @AuthenticationPrincipal Jwt jwt) {
        return queries.trials(tenantId, candidateId, JwtIdentity.from(jwt));
    }

    @PostMapping("/candidates/{candidateId}:transition")
    CandidateResponse transition(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable @Pattern(regexp = "[0-9a-f]{64}") String candidateId,
            @Valid @RequestBody TransitionRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        return CandidateResponse.from(candidates.transition(
                tenantId,
                candidateId,
                request.expectedVersion(),
                EvolutionCandidate.Status.fromWireValue(request.targetStatus()),
                request.reason(),
                JwtIdentity.from(jwt)));
    }

    record TransitionRequest(
            @Positive long expectedVersion,
            @NotBlank @Pattern(regexp = "eligible|canary|promoted|rejected|rolled_back|stale")
                    String targetStatus,
            @NotBlank @Size(max = 2000) String reason) {
    }

    record CandidateResponse(
            String candidateId,
            String baseHarnessVersionId,
            String baseManifestDigest,
            String failureSignature,
            String step,
            String checkName,
            String category,
            String requiredCapability,
            List<String> profileReferences,
            List<String> observationIds,
            int occurrenceCount,
            int projectCount,
            String riskTier,
            String changeKind,
            String status,
            String transitionReason,
            long rowVersion,
            Instant createdAt,
            Instant updatedAt) {

        static CandidateResponse from(EvolutionCandidate value) {
            return new CandidateResponse(
                    value.candidateId(),
                    value.baseHarnessVersionId(),
                    value.baseManifestDigest(),
                    value.failureSignature(),
                    value.step(),
                    value.checkName(),
                    value.category(),
                    value.requiredCapability(),
                    value.profileReferences(),
                    value.observationIds(),
                    value.occurrenceCount(),
                    value.projectCount(),
                    value.riskTier(),
                    value.changeKind(),
                    value.status().wireValue(),
                    value.transitionReason(),
                    value.rowVersion(),
                    value.createdAt(),
                    value.updatedAt());
        }
    }
}
