package team.ratsnest.controlplane.run.application;

import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.agentgateway.domain.model.AgentRuntimeException;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.HistoryQuery;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeInfo;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeMessage;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.run.application.model.RunRuntimeStatus;
import team.ratsnest.controlplane.run.domain.model.ConversationSummary;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.run.domain.port.RunStore;
import team.ratsnest.controlplane.shared.web.ApiException;

/** Read-side and runtime-status use cases for runs and conversations. */
@Service
public class RunQueryService {

    private final TransactionTemplate transactions;
    private final RunStore runs;
    private final AgentRuntimeGateway runtime;
    private final RunAccessSupport access;
    private final RunLifecycleService lifecycle;

    public RunQueryService(
            TransactionTemplate transactions,
            RunStore runs,
            AgentRuntimeGateway runtime,
            RunAccessSupport access,
            RunLifecycleService lifecycle) {
        this.transactions = transactions;
        this.runs = runs;
        this.runtime = runtime;
        this.access = access;
        this.lifecycle = lifecycle;
    }

    public Run get(UUID tenantId, UUID runId, AuthenticatedActor actor) {
        Run run = access.requireRun(tenantId, runId, actor);
        if (RunAccessSupport.terminal(run.state())) {
            lifecycle.synchronizeTerminalResult(run);
            return access.requireRun(tenantId, runId, actor);
        }
        try {
            RuntimeRun current = runtime.getRun(access.reference(run));
            lifecycle.updateFromRuntime(run, current);
            return access.requireRun(tenantId, runId, actor);
        } catch (AgentRuntimeException exception) {
            return run;
        }
    }

    public Run authorizeRead(UUID tenantId, UUID runId, AuthenticatedActor actor) {
        return access.requireRun(tenantId, runId, actor);
    }

    public RunRuntimeStatus runtimeStatus(
            UUID tenantId,
            UUID runId,
            AuthenticatedActor actor) {
        Run run = access.requireRun(tenantId, runId, actor);
        try {
            return RunRuntimeStatus.from(run, runtime.getRun(access.reference(run)), false);
        } catch (AgentRuntimeException exception) {
            return RunRuntimeStatus.from(run, null, false);
        }
    }

    public List<RuntimeMessage> history(
            UUID tenantId,
            UUID projectId,
            String threadId,
            AuthenticatedActor actor) {
        RuntimeIdentity identity = transactions.execute(status -> {
            RuntimeIdentity value = access.requireProject(tenantId, projectId, actor, false);
            if (runs.isConversationRemoved(tenantId, projectId, threadId, actor)) {
                throw access.conversationNotFound();
            }
            return value;
        });
        if (identity == null) {
            throw access.notFound();
        }
        try {
            return runtime.getHistory(new HistoryQuery(threadId, identity));
        } catch (AgentRuntimeException exception) {
            throw access.runtimeFailure(exception);
        }
    }

    public List<ConversationSummary> conversations(
            UUID tenantId,
            UUID projectId,
            AuthenticatedActor actor) {
        List<ConversationSummary> result = transactions.execute(status -> {
            access.requireProject(tenantId, projectId, actor, false);
            return runs.listConversations(tenantId, projectId, 100);
        });
        return result == null ? List.of() : result;
    }

    public void removeConversation(
            UUID tenantId,
            UUID projectId,
            String threadId,
            AuthenticatedActor actor) {
        transactions.executeWithoutResult(status -> {
            access.requireProject(tenantId, projectId, actor, false);
            Run latest = runs.findLatestForThread(tenantId, projectId, threadId)
                    .orElseThrow(access::conversationNotFound);
            if (latest.state() == RunState.QUEUED
                    || latest.state() == RunState.RUNNING
                    || latest.state() == RunState.WAITING_FOR_INPUT) {
                throw new ApiException(
                        "CONVERSATION_ACTIVE",
                        HttpStatus.CONFLICT,
                        "An active conversation must finish or be cancelled before deletion.");
            }
            runs.removeConversation(tenantId, projectId, threadId, actor);
        });
    }

    public RuntimeInfo info(
            UUID tenantId,
            UUID projectId,
            AuthenticatedActor actor) {
        RuntimeIdentity identity = transactions.execute(
                status -> access.requireProject(tenantId, projectId, actor, false));
        if (identity == null) {
            throw access.notFound();
        }
        try {
            return runtime.getInfo(identity);
        } catch (AgentRuntimeException exception) {
            throw access.runtimeFailure(exception);
        }
    }
}
