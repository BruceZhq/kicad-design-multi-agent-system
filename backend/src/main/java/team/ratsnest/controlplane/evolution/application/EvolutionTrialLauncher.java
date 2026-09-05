package team.ratsnest.controlplane.evolution.application;

import java.util.List;
import java.util.UUID;

import org.springframework.stereotype.Service;

import team.ratsnest.controlplane.evolution.application.EvolutionTrialService.EvaluateCommand;
import team.ratsnest.controlplane.evolution.application.EvolutionTrialService.PreparedProposal;
import team.ratsnest.controlplane.evolution.application.EvolutionTrialService.PreparedTrial;
import team.ratsnest.controlplane.evolution.domain.model.EvolutionTrial;
import team.ratsnest.controlplane.evolution.domain.port.EvolutionTrialRuntime;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;

/** Coordinates the transactional trial state with the external runtime launch. */
@Service
public class EvolutionTrialLauncher {

    private final EvolutionTrialService evolution;
    private final EvolutionTrialRuntime runtime;

    public EvolutionTrialLauncher(
            EvolutionTrialService evolution,
            EvolutionTrialRuntime runtime) {
        this.evolution = evolution;
        this.runtime = runtime;
    }

    public EvolutionTrial evaluate(
            UUID tenantId,
            String candidateId,
            long expectedVersion,
            String idempotencyKey,
            EvaluateCommand command,
            AuthenticatedActor actor) {
        PreparedTrial prepared = evolution.prepareTrial(
                tenantId,
                candidateId,
                expectedVersion,
                idempotencyKey,
                command,
                actor);
        if (!prepared.needsStart()) {
            return prepared.trial();
        }
        String workflowId = "ratsnest-evolution-" + prepared.trial().trialId();
        EvolutionTrial bound = evolution.bindWorkflow(
                tenantId, prepared.trial().trialId(), workflowId);
        EvolutionTrialRuntime.StartResult started = runtime.start(
                tenantId, bound, prepared.trialInput());
        return evolution.bindWorkflow(tenantId, bound.trialId(), started.workflowId());
    }

    public EvolutionTrial proposeAndEvaluate(
            UUID tenantId,
            String candidateId,
            long expectedVersion,
            String idempotencyKey,
            List<String> repositoryContextPaths,
            AuthenticatedActor actor) {
        PreparedProposal prepared = evolution.prepareProposal(
                tenantId,
                candidateId,
                expectedVersion,
                idempotencyKey,
                repositoryContextPaths);
        EvolutionTrialRuntime.ProposalResult proposal = prepared.needsGeneration()
                ? evolution.persistProposalResult(
                        tenantId,
                        prepared,
                        runtime.propose(tenantId, prepared.proposalId(), prepared.request()))
                : prepared.cachedResult();
        EvaluateCommand command = evolution.commandFromProposal(prepared, proposal);
        return evaluate(
                tenantId,
                candidateId,
                expectedVersion,
                idempotencyKey,
                command,
                actor);
    }
}
