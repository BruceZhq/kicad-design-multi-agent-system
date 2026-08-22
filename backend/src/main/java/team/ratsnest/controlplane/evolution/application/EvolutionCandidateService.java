package team.ratsnest.controlplane.evolution.application;

import java.util.Map;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.application.TenantAccess;
import team.ratsnest.controlplane.tenancy.domain.model.MembershipRole;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

/** State-transition use cases for governed-evolution candidates. */
@Service
public class EvolutionCandidateService {

    private final TenantAccess tenantAccess;
    private final TenantContext tenantContext;
    private final EvolutionRepository evolution;

    public EvolutionCandidateService(
            TenantAccess tenantAccess,
            TenantContext tenantContext,
            EvolutionRepository evolution) {
        this.tenantAccess = tenantAccess;
        this.tenantContext = tenantContext;
        this.evolution = evolution;
    }

    @Transactional
    public EvolutionCandidate transition(
            UUID tenantId,
            String candidateId,
            long expectedVersion,
            EvolutionCandidate.Status target,
            String reason,
            AuthenticatedActor actor) {
        MembershipRole role = tenantAccess.requireMembership(tenantId, actor);
        if (!role.canManageEvolution()) {
            throw new ApiException(
                    "EVOLUTION_TRANSITION_DENIED",
                    HttpStatus.FORBIDDEN,
                    "Only organization owners and administrators can transition evolution candidates.");
        }
        EvolutionCandidate candidate = requireCandidate(tenantId, candidateId);
        if (candidate.rowVersion() != expectedVersion) {
            throw stale();
        }
        if (target == EvolutionCandidate.Status.EVALUATING
                || target == EvolutionCandidate.Status.AWAITING_APPROVAL
                || target == EvolutionCandidate.Status.APPROVED
                || candidate.status() == EvolutionCandidate.Status.EVALUATING) {
            throw new ApiException(
                    "EVOLUTION_EVALUATION_PROOF_REQUIRED",
                    HttpStatus.CONFLICT,
                    "Use the governed evaluation and platform approval APIs for proof-bound states.");
        }
        if (!candidate.status().canTransitionTo(target)) {
            throw new ApiException(
                    "EVOLUTION_TRANSITION_INVALID",
                    HttpStatus.CONFLICT,
                    "The requested evolution candidate transition is not allowed.");
        }
        if (!evolution.transition(tenantId, candidate, target, reason.strip(), actor)) {
            throw stale();
        }
        return requireCandidate(tenantId, candidateId);
    }

    @Transactional
    public EvolutionCandidate approveCandidate(
            UUID tenantId,
            String candidateId,
            long expectedVersion,
            UUID trialId,
            String reportDigest,
            String reason,
            AuthenticatedActor actor) {
        tenantContext.activate(tenantId);
        EvolutionCandidate candidate = requireCandidate(tenantId, candidateId);
        EvolutionTrial trial = requireTrial(tenantId, trialId);
        if (candidate.status() == EvolutionCandidate.Status.APPROVED
                && candidate.rowVersion() - 1 == expectedVersion
                && approvalProofMatches(candidateId, reportDigest, trial)) {
            return candidate;
        }
        if (candidate.rowVersion() != expectedVersion) {
            throw stale();
        }
        if (candidate.status() != EvolutionCandidate.Status.AWAITING_APPROVAL) {
            throw new ApiException(
                    "EVOLUTION_APPROVAL_INVALID",
                    HttpStatus.CONFLICT,
                    "Only a candidate awaiting approval can be approved.");
        }
        if (!approvalProofMatches(candidateId, reportDigest, trial)) {
            throw new ApiException(
                    "EVOLUTION_APPROVAL_PROOF_INVALID",
                    HttpStatus.CONFLICT,
                    "Approval requires the exact completed passing Trial proof.");
        }
        if (!evolution.transition(
                tenantId,
                candidate,
                EvolutionCandidate.Status.APPROVED,
                reason.strip(),
                actor)) {
            throw stale();
        }
        return requireCandidate(tenantId, candidateId);
    }

    private boolean approvalProofMatches(
            String candidateId,
            String reportDigest,
            EvolutionTrial trial) {
        Map<String, Object> guardrails = trial.guardrailResults();
        return candidateId.equals(trial.candidateId())
                && "PASSED".equals(trial.verdict())
                && reportDigest.equals(trial.reportDigest())
                && trial.completedAt() != null
                && !trial.authoritativeReport().isEmpty()
                && Boolean.TRUE.equals(guardrails.get("runtimeAttested"))
                && Boolean.TRUE.equals(guardrails.get("guardrailPassed"))
                && Boolean.TRUE.equals(guardrails.get("authoritativeGatePassed"));
    }

    private EvolutionTrial requireTrial(UUID tenantId, UUID trialId) {
        return evolution.findTrial(tenantId, trialId).orElseThrow(() -> new ApiException(
                "EVOLUTION_TRIAL_NOT_FOUND",
                HttpStatus.NOT_FOUND,
                "The evolution trial was not found."));
    }

    private EvolutionCandidate requireCandidate(UUID tenantId, String candidateId) {
        return evolution.findCandidate(tenantId, candidateId).orElseThrow(() -> new ApiException(
                "EVOLUTION_CANDIDATE_NOT_FOUND",
                HttpStatus.NOT_FOUND,
                "The evolution candidate was not found."));
    }

    private ApiException stale() {
        return new ApiException(
                "EVOLUTION_CANDIDATE_STALE",
                HttpStatus.CONFLICT,
                "The evolution candidate changed; reload it before retrying the transition.");
    }
}
