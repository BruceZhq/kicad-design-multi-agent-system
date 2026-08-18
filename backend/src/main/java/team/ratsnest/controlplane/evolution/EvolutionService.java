package team.ratsnest.controlplane.evolution;

import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.MembershipRole;
import team.ratsnest.controlplane.tenancy.TenantAccess;

@Service
public class EvolutionService {

    private final TenantAccess tenantAccess;
    private final EvolutionRepository evolution;

    public EvolutionService(TenantAccess tenantAccess, EvolutionRepository evolution) {
        this.tenantAccess = tenantAccess;
        this.evolution = evolution;
    }

    @Transactional(readOnly = true)
    public List<EvolutionObservation> observations(UUID tenantId, AuthenticatedActor actor) {
        tenantAccess.requireMembership(tenantId, actor);
        return evolution.findObservations(tenantId);
    }

    @Transactional(readOnly = true)
    public List<EvolutionCandidate> candidates(UUID tenantId, AuthenticatedActor actor) {
        tenantAccess.requireMembership(tenantId, actor);
        return evolution.findCandidates(tenantId);
    }

    @Transactional(readOnly = true)
    public EvolutionCandidate candidate(
            UUID tenantId,
            String candidateId,
            AuthenticatedActor actor) {
        tenantAccess.requireMembership(tenantId, actor);
        return requireCandidate(tenantId, candidateId);
    }

    @Transactional(readOnly = true)
    public List<EvolutionTrial> trials(
            UUID tenantId,
            String candidateId,
            AuthenticatedActor actor) {
        tenantAccess.requireMembership(tenantId, actor);
        requireCandidate(tenantId, candidateId);
        return evolution.findTrials(tenantId, candidateId);
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
        if (target == EvolutionCandidate.Status.AWAITING_APPROVAL
                || target == EvolutionCandidate.Status.APPROVED) {
            throw new ApiException(
                    "EVOLUTION_EVALUATION_PROOF_REQUIRED",
                    HttpStatus.CONFLICT,
                    "Only the governed evaluation result path may enter awaiting_approval or approved; that path is not enabled yet.");
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
