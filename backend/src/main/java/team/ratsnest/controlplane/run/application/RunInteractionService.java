package team.ratsnest.controlplane.run.application;

import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.agentgateway.domain.model.AgentRuntimeException;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.ResumeRunCommand;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.project.application.ProjectService;
import team.ratsnest.controlplane.run.application.model.RunRuntimeStatus;
import team.ratsnest.controlplane.run.application.model.RunRuntimeStatus.RunExecutionStatus;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.run.domain.model.RunInteraction;
import team.ratsnest.controlplane.run.domain.port.RunInteractionStore;
import team.ratsnest.controlplane.run.domain.port.RunStore;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.application.TenantAccess;
import team.ratsnest.controlplane.tenancy.domain.model.MembershipRole;
import team.ratsnest.controlplane.tenancy.domain.port.TenantContext;

/** Human-in-the-loop response and interrupted-run recovery use cases. */
@Service
public class RunInteractionService {

    private final TransactionTemplate transactions;
    private final TenantAccess tenantAccess;
    private final TenantContext tenantContext;
    private final ProjectService projects;
    private final RunStore runs;
    private final RunInteractionStore interactions;
    private final AgentRuntimeGateway runtime;
    private final RunAccessSupport access;
    private final RunLifecycleService lifecycle;
    private final RunRequestFingerprint fingerprints;

    public RunInteractionService(
            TransactionTemplate transactions,
            TenantAccess tenantAccess,
            TenantContext tenantContext,
            ProjectService projects,
            RunStore runs,
            RunInteractionStore interactions,
            AgentRuntimeGateway runtime,
            RunAccessSupport access,
            RunLifecycleService lifecycle,
            RunRequestFingerprint fingerprints) {
        this.transactions = transactions;
        this.tenantAccess = tenantAccess;
        this.tenantContext = tenantContext;
        this.projects = projects;
        this.runs = runs;
        this.interactions = interactions;
        this.runtime = runtime;
        this.access = access;
        this.lifecycle = lifecycle;
        this.fingerprints = fingerprints;
    }

    public RunRuntimeStatus recover(
            UUID tenantId,
            UUID runId,
            AuthenticatedActor actor) {
        Run run = access.requireWritableRun(tenantId, runId, actor);
        if (RunAccessSupport.terminal(run.state())) {
            throw new ApiException(
                    "RUN_TERMINAL_USE_REVISION",
                    HttpStatus.CONFLICT,
                    "A terminal run must be continued by creating a revision.");
        }
        if (run.state() == RunState.WAITING_FOR_INPUT) {
            throw new ApiException(
                    "RUN_WAITING_FOR_INPUT",
                    HttpStatus.CONFLICT,
                    "The run is waiting for an interaction response.");
        }

        try {
            RuntimeRun current = runtime.getRun(access.reference(run));
            RunRuntimeStatus status = RunRuntimeStatus.from(run, current, false);
            if (status.executionStatus() == RunExecutionStatus.TERMINAL) {
                lifecycle.updateFromRuntime(run, current);
                throw new ApiException(
                        "RUN_TERMINAL_USE_REVISION",
                        HttpStatus.CONFLICT,
                        "A terminal run must be continued by creating a revision.");
            }
            if (status.executionStatus() == RunExecutionStatus.WAITING_FOR_INPUT) {
                lifecycle.updateFromRuntime(run, current);
                throw new ApiException(
                        "RUN_WAITING_FOR_INPUT",
                        HttpStatus.CONFLICT,
                        "The run is waiting for an interaction response.");
            }
            if (status.executionStatus() == RunExecutionStatus.ACTIVE
                    || status.executionStatus() == RunExecutionStatus.RECOVERING) {
                throw new ApiException(
                        "RUN_ALREADY_ACTIVE",
                        HttpStatus.CONFLICT,
                        "The Agent Runtime still owns an active execution lease.");
            }
            if (status.executionStatus() != RunExecutionStatus.RECOVERABLE) {
                throw new ApiException(
                        "RUN_NOT_RECOVERABLE",
                        HttpStatus.CONFLICT,
                        "The Agent Runtime has not confirmed that this run can be recovered.");
            }

            RuntimeRun recovered = runtime.startRun(access.command(run));
            lifecycle.updateFromRuntime(run, recovered);
            return RunRuntimeStatus.from(
                    access.requireRun(tenantId, runId, actor), recovered, true);
        } catch (AgentRuntimeException exception) {
            throw access.runtimeFailure(exception);
        }
    }

    public Run respond(
            UUID tenantId,
            UUID runId,
            String interactionId,
            String idempotencyKey,
            String answer,
            long stateVersion,
            AuthenticatedActor actor) {
        InteractionResponse response = transactions.execute(status -> beginInteractionResponse(
                tenantId,
                runId,
                interactionId,
                idempotencyKey,
                answer,
                stateVersion,
                actor));
        if (response == null) {
            throw access.notFound();
        }
        Run run = response.run();
        RunInteraction interaction = response.interaction();
        if (!response.dispatch() && interaction.status() == RunInteraction.Status.RESPONDED) {
            return run;
        }
        try {
            RuntimeRun resumed = runtime.resumeRun(new ResumeRunCommand(
                    access.reference(run),
                    interaction.interactionId(),
                    interaction.responseRequestId().toString(),
                    interaction.answer(),
                    interaction.stateVersion(),
                    run.model(),
                    null,
                    run.runtimeConfig()));
            lifecycle.updateFromRuntime(run, resumed);
            transactions.executeWithoutResult(status -> {
                tenantContext.activate(tenantId);
                RunInteraction current = interactions.findForUpdate(
                                tenantId, runId, interactionId)
                        .orElseThrow(access::notFound);
                if (current.status() == RunInteraction.Status.RESPONDING
                        && current.responseRequestId().equals(interaction.responseRequestId())) {
                    interactions.markResponded(current);
                    lifecycle.appendLifecycle(
                            run, "run.interaction.responded", resumed.state(), null, null);
                }
            });
            return access.requireRun(tenantId, runId, actor);
        } catch (AgentRuntimeException exception) {
            // RESPONDING remains durable; replay retries the same request ID.
            throw access.runtimeFailure(exception);
        }
    }

    private InteractionResponse beginInteractionResponse(
            UUID tenantId,
            UUID runId,
            String interactionId,
            String idempotencyKey,
            String answer,
            long stateVersion,
            AuthenticatedActor actor) {
        MembershipRole role = tenantAccess.requireMembership(tenantId, actor);
        if (!role.canWriteProjects()) {
            throw new ApiException(
                    "RUN_INTERACTION_DENIED",
                    HttpStatus.FORBIDDEN,
                    "The organization role cannot respond to run interactions.");
        }
        Run run = runs.findForUpdate(tenantId, runId).orElseThrow(access::notFound);
        projects.get(tenantId, run.projectId(), actor);
        RunInteraction interaction = interactions.findForUpdate(tenantId, runId, interactionId)
                .orElseThrow(() -> new ApiException(
                        "INTERACTION_NOT_FOUND",
                        HttpStatus.NOT_FOUND,
                        "The requested run interaction was not found."));
        if (Boolean.FALSE.equals(interaction.request().get("allowFreeText"))
                && interaction.request().get("options") instanceof List<?> options
                && !options.contains(answer)) {
            throw new ApiException(
                    "INTERACTION_ANSWER_INVALID",
                    HttpStatus.BAD_REQUEST,
                    "The answer must be one of the interaction options.");
        }
        String fingerprint = fingerprints.interaction(interactionId, answer, stateVersion);
        if (interaction.status() != RunInteraction.Status.PENDING) {
            if (idempotencyKey.equals(interaction.responseIdempotencyKey())
                    && fingerprint.equals(interaction.responseFingerprint())) {
                return new InteractionResponse(
                        run,
                        interaction,
                        interaction.status() == RunInteraction.Status.RESPONDING);
            }
            throw new ApiException(
                    "INTERACTION_ALREADY_RESPONDED",
                    HttpStatus.CONFLICT,
                    "The interaction already has a different response.");
        }
        if (interaction.stateVersion() != stateVersion
                || run.state() != RunState.WAITING_FOR_INPUT) {
            throw new ApiException(
                    "INTERACTION_STALE",
                    HttpStatus.CONFLICT,
                    "The interaction state version is no longer current.");
        }
        UUID responseRequestId = UUID.randomUUID();
        if (!interactions.beginResponse(
                interaction,
                idempotencyKey,
                fingerprint,
                responseRequestId,
                answer,
                actor)) {
            throw new ApiException(
                    "INTERACTION_STALE",
                    HttpStatus.CONFLICT,
                    "The interaction was changed concurrently.");
        }
        return new InteractionResponse(
                run,
                new RunInteraction(
                        interaction.tenantId(),
                        interaction.interactionId(),
                        interaction.runId(),
                        interaction.kind(),
                        interaction.stateVersion(),
                        interaction.request(),
                        RunInteraction.Status.RESPONDING,
                        idempotencyKey,
                        fingerprint,
                        responseRequestId,
                        answer),
                true);
    }

    private record InteractionResponse(Run run, RunInteraction interaction, boolean dispatch) {
    }
}
