package team.ratsnest.controlplane.run.application;

import java.util.List;
import java.util.UUID;

import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.agentgateway.domain.model.AgentRuntimeException;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.CapabilityProfile;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunReference;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeInfo;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.StartRunCommand;
import team.ratsnest.controlplane.agentgateway.domain.port.RuntimeCredentials;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.project.application.ProjectService;
import team.ratsnest.controlplane.run.application.model.ProfileSelector;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.run.domain.port.RunStore;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.application.TenantAccess;
import team.ratsnest.controlplane.tenancy.domain.model.MembershipRole;

/** Shared authorization and Runtime identity policy for run use cases. */
@Component
class RunAccessSupport {

    private final TransactionTemplate transactions;
    private final TenantAccess tenantAccess;
    private final ProjectService projects;
    private final RunStore runs;
    private final AgentRuntimeGateway runtime;
    private final RuntimeCredentials signer;

    RunAccessSupport(
            TransactionTemplate transactions,
            TenantAccess tenantAccess,
            ProjectService projects,
            RunStore runs,
            AgentRuntimeGateway runtime,
            RuntimeCredentials signer) {
        this.transactions = transactions;
        this.tenantAccess = tenantAccess;
        this.projects = projects;
        this.runs = runs;
        this.runtime = runtime;
        this.signer = signer;
    }

    Run requireRun(UUID tenantId, UUID runId, AuthenticatedActor actor) {
        Run result = transactions.execute(status -> {
            tenantAccess.requireMembership(tenantId, actor);
            return runs.find(tenantId, runId).orElseThrow(this::notFound);
        });
        if (result == null) {
            throw notFound();
        }
        return result;
    }

    Run requireWritableRun(UUID tenantId, UUID runId, AuthenticatedActor actor) {
        Run result = transactions.execute(status -> {
            MembershipRole role = tenantAccess.requireMembership(tenantId, actor);
            if (!role.canWriteProjects()) {
                throw new ApiException(
                        "RUN_CONTROL_DENIED",
                        HttpStatus.FORBIDDEN,
                        "The organization role cannot control runs.");
            }
            return runs.find(tenantId, runId).orElseThrow(this::notFound);
        });
        if (result == null) {
            throw notFound();
        }
        return result;
    }

    RuntimeIdentity requireProject(
            UUID tenantId,
            UUID projectId,
            AuthenticatedActor actor,
            boolean write) {
        MembershipRole role = tenantAccess.requireMembership(tenantId, actor);
        if (write && !role.canWriteProjects()) {
            throw new ApiException(
                    "RUN_START_DENIED",
                    HttpStatus.FORBIDDEN,
                    "The organization role cannot start runs.");
        }
        projects.get(tenantId, projectId, actor);
        return identity(tenantId, projectId, actor);
    }

    CapabilityProfile resolveProfile(
            UUID tenantId,
            UUID projectId,
            ProfileSelector selector,
            AuthenticatedActor actor) {
        RuntimeIdentity identity = transactions.execute(
                status -> requireProject(tenantId, projectId, actor, true));
        if (identity == null) {
            throw notFound();
        }
        RuntimeInfo info;
        try {
            info = runtime.getInfo(identity);
        } catch (AgentRuntimeException exception) {
            throw runtimeFailure(exception);
        }
        List<CapabilityProfile> matches = info.profiles().stream()
                .filter(profile -> profile.id().equals(selector.id()))
                .filter(profile -> profile.version().equals(selector.version()))
                .toList();
        if (matches.isEmpty()) {
            throw new ApiException(
                    "CAPABILITY_PROFILE_NOT_AVAILABLE",
                    HttpStatus.BAD_REQUEST,
                    "The requested capability profile is not available.");
        }
        if (matches.size() != 1) {
            throw new ApiException(
                    "AGENT_RUNTIME_ERROR",
                    HttpStatus.BAD_GATEWAY,
                    "Agent Runtime returned duplicate capability profiles.");
        }
        return matches.getFirst();
    }

    StartRunCommand command(Run run) {
        return new StartRunCommand(
                run.runId().toString(),
                run.threadId(),
                run.runtimeIdentity(signer),
                run.message(),
                run.model(),
                null,
                run.runtimeConfig(),
                true);
    }

    RunReference reference(Run run) {
        return new RunReference(
                run.runId().toString(),
                run.runtimeIdentity(signer),
                run.harnessChannel());
    }

    RuntimeIdentity identity(UUID tenantId, UUID projectId, AuthenticatedActor actor) {
        return new RuntimeIdentity(
                signer.principalId(tenantId, projectId, actor),
                tenantId.toString(),
                projectId.toString());
    }

    ApiException runtimeFailure(AgentRuntimeException exception) {
        HttpStatus status = exception.status() == 404
                ? HttpStatus.NOT_FOUND
                : exception.status() == 409
                        ? HttpStatus.CONFLICT
                        : HttpStatus.BAD_GATEWAY;
        return new ApiException("AGENT_RUNTIME_ERROR", status, exception.getMessage());
    }

    ApiException notFound() {
        return new ApiException("RUN_NOT_FOUND", HttpStatus.NOT_FOUND, "The run was not found.");
    }

    ApiException conversationNotFound() {
        return new ApiException(
                "CONVERSATION_NOT_FOUND",
                HttpStatus.NOT_FOUND,
                "The conversation was not found.");
    }

    static boolean terminal(RunState state) {
        return state == RunState.COMPLETED
                || state == RunState.FAILED
                || state == RunState.CANCELLED
                || state == RunState.TIMED_OUT;
    }
}
