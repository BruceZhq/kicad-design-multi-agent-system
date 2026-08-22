package team.ratsnest.controlplane.evolution.application;

import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import team.ratsnest.controlplane.evolution.domain.model.EvolutionCandidate;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionObservation;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionRepository;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.application.TenantAccess;

/** Read-side use cases for tenant-scoped evolution state. */
@Service
public class EvolutionQueryService {

    private final TenantAccess tenantAccess;
    private final EvolutionRepository evolution;

    public EvolutionQueryService(TenantAccess tenantAccess, EvolutionRepository evolution) {
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

    private EvolutionCandidate requireCandidate(UUID tenantId, String candidateId) {
        return evolution.findCandidate(tenantId, candidateId).orElseThrow(() -> new ApiException(
                "EVOLUTION_CANDIDATE_NOT_FOUND",
                HttpStatus.NOT_FOUND,
                "The evolution candidate was not found."));
    }
}
