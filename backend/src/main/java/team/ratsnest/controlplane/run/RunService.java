package team.ratsnest.controlplane.run;

import java.security.MessageDigest;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.UUID;
import java.util.concurrent.Flow;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.agentgateway.AgentRuntimeException;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.CapabilityProfile;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.ControlRunCommand;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.EventSubscription;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.HistoryQuery;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RunControl;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RunReference;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.ResumeRunCommand;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeInfo;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeMessage;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.StartRunCommand;
import team.ratsnest.controlplane.agentgateway.InternalTaskSigner;
import team.ratsnest.controlplane.artifact.ArtifactManifest;
import team.ratsnest.controlplane.artifact.ArtifactManifestParser;
import team.ratsnest.controlplane.artifact.ArtifactRepository;
import team.ratsnest.controlplane.evolution.EvolutionCollector;
import team.ratsnest.controlplane.harness.HarnessReleaseRouter;
import team.ratsnest.controlplane.harness.HarnessReleaseRouter.HarnessSelection;
import team.ratsnest.controlplane.harness.HarnessVersion;
import team.ratsnest.controlplane.harness.HarnessVersionService;
import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.project.ProjectService;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.MembershipRole;
import team.ratsnest.controlplane.tenancy.TenantAccess;
import team.ratsnest.controlplane.tenancy.TenantContext;
import tools.jackson.databind.ObjectMapper;

@Service
public class RunService {

    private static final Logger logger = LoggerFactory.getLogger(RunService.class);

    private final TransactionTemplate transactions;
    private final TenantAccess tenantAccess;
    private final TenantContext tenantContext;
    private final ProjectService projects;
    private final RunRepository runs;
    private final RunInteractionRepository interactions;
    private final RunOutboxRepository outbox;
    private final ArtifactRepository artifacts;
    private final ArtifactManifestParser artifactManifests;
    private final EvolutionCollector evolutionCollector;
    private final HarnessVersionService harnessVersions;
    private final HarnessReleaseRouter harnessReleaseRouter;
    private final AgentRuntimeGateway runtime;
    private final InternalTaskSigner signer;
    private final ObjectMapper objectMapper;
    private final boolean outboxEnabled;
    private final boolean reconciliationEnabled;

    public RunService(
            TransactionTemplate transactions,
            TenantAccess tenantAccess,
            TenantContext tenantContext,
            ProjectService projects,
            RunRepository runs,
            RunInteractionRepository interactions,
            RunOutboxRepository outbox,
            ArtifactRepository artifacts,
            ArtifactManifestParser artifactManifests,
            EvolutionCollector evolutionCollector,
            HarnessVersionService harnessVersions,
            HarnessReleaseRouter harnessReleaseRouter,
            AgentRuntimeGateway runtime,
            InternalTaskSigner signer,
            ObjectMapper objectMapper,
            @Value("${ratsnest.run-outbox.enabled:false}") boolean outboxEnabled,
            @Value("${ratsnest.run-reconciliation.enabled:false}") boolean reconciliationEnabled) {
        this.transactions = transactions;
        this.tenantAccess = tenantAccess;
        this.tenantContext = tenantContext;
        this.projects = projects;
        this.runs = runs;
        this.interactions = interactions;
        this.outbox = outbox;
        this.artifacts = artifacts;
        this.artifactManifests = artifactManifests;
        this.evolutionCollector = evolutionCollector;
        this.harnessVersions = harnessVersions;
        this.harnessReleaseRouter = harnessReleaseRouter;
        this.runtime = runtime;
        this.signer = signer;
        this.objectMapper = objectMapper;
        this.outboxEnabled = outboxEnabled;
        this.reconciliationEnabled = reconciliationEnabled;
    }

    public Run start(
            UUID tenantId,
            UUID projectId,
            String idempotencyKey,
            StartRequest request,
            AuthenticatedActor actor) {
        Run replay = transactions.execute(status -> existingBeforeRuntime(
                tenantId,
                projectId,
                idempotencyKey,
                request,
                actor));
        if (replay != null) {
            return replay;
        }
        CapabilityProfile profile = resolveProfile(
                tenantId,
                projectId,
                request.capabilityProfile(),
                actor);
        HarnessSelection harness = harnessReleaseRouter.route(
                tenantId, projectId, idempotencyKey);
        Map<String, Object> config = runtimeConfig(request.teamMembers(), profile, harness);
        String fingerprint = fingerprint(
                tenantId,
                projectId,
                request.threadId(),
                request,
                config);
        String threadId = request.threadId() == null
                ? UUID.randomUUID().toString()
                : request.threadId();

        Creation creation;
        try {
            creation = transactions.execute(status -> createOrGet(
                    tenantId,
                    projectId,
                    idempotencyKey,
                    threadId,
                    request,
                    config,
                    profile,
                    harness,
                    fingerprint,
                    actor));
        } catch (DataIntegrityViolationException exception) {
            creation = transactions.execute(status -> existing(
                    tenantId,
                    projectId,
                    idempotencyKey,
                    fingerprint,
                    actor));
        }
        if (creation == null) {
            throw new IllegalStateException("Run transaction returned no result");
        }
        if (!creation.created()) {
            return creation.run();
        }

        Run run = creation.run();
        if (reconciliationEnabled) {
            return requireRun(tenantId, run.runId(), actor);
        }
        try {
            RuntimeRun started = runtime.startRun(command(run));
            updateFromRuntime(run, started);
        } catch (AgentRuntimeException exception) {
            markFailed(run, "RUNTIME_START_FAILED", exception.getMessage());
        }
        return requireRun(tenantId, run.runId(), actor);
    }

    public Run get(UUID tenantId, UUID runId, AuthenticatedActor actor) {
        Run run = requireRun(tenantId, runId, actor);
        if (terminal(run.state())) {
            synchronizeTerminalResult(run);
            return requireRun(tenantId, runId, actor);
        }
        try {
            RuntimeRun current = runtime.getRun(reference(run));
            updateFromRuntime(run, current);
            return requireRun(tenantId, runId, actor);
        } catch (AgentRuntimeException exception) {
            return run;
        }
    }

    public Run authorizeRead(UUID tenantId, UUID runId, AuthenticatedActor actor) {
        return requireRun(tenantId, runId, actor);
    }

    public Run revise(
            UUID tenantId,
            UUID runId,
            String idempotencyKey,
            String feedback,
            AuthenticatedActor actor) {
        Creation creation;
        try {
            creation = transactions.execute(status -> createOrGetRevision(
                    tenantId, runId, idempotencyKey, feedback, actor));
        } catch (DataIntegrityViolationException exception) {
            Run parent = requireWritableRun(tenantId, runId, actor);
            String fingerprint = revisionFingerprint(parent, feedback);
            creation = transactions.execute(status -> existing(
                    tenantId, parent.projectId(), idempotencyKey, fingerprint, actor));
        }
        if (creation == null) {
            throw new IllegalStateException("Run revision transaction returned no result");
        }
        if (!creation.created()) {
            return creation.run();
        }
        Run revision = creation.run();
        if (reconciliationEnabled) {
            return requireRun(tenantId, revision.runId(), actor);
        }
        try {
            updateFromRuntime(revision, runtime.startRun(command(revision)));
        } catch (AgentRuntimeException exception) {
            markFailed(revision, "RUNTIME_START_FAILED", exception.getMessage());
        }
        return requireRun(tenantId, revision.runId(), actor);
    }

    public Run cancel(UUID tenantId, UUID runId, AuthenticatedActor actor) {
        Run run = requireWritableRun(tenantId, runId, actor);
        if (terminal(run.state())) {
            return run;
        }
        try {
            if (reconciliationEnabled && run.state() == RunState.QUEUED) {
                // The reconciler may not have created the idempotent Runtime
                // record yet; establish it before sending the cancellation.
                runtime.startRun(command(run));
            }
            RuntimeRun cancelled = runtime.controlRun(
                    new ControlRunCommand(reference(run), RunControl.CANCEL));
            updateFromRuntime(run, cancelled);
            return requireRun(tenantId, runId, actor);
        } catch (AgentRuntimeException exception) {
            throw runtimeFailure(exception);
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
            throw notFound();
        }
        Run run = response.run();
        RunInteraction interaction = response.interaction();
        if (!response.dispatch() && interaction.status() == RunInteraction.Status.RESPONDED) {
            return run;
        }
        try {
            RuntimeRun resumed = runtime.resumeRun(new ResumeRunCommand(
                    reference(run),
                    interaction.interactionId(),
                    interaction.responseRequestId().toString(),
                    interaction.answer(),
                    interaction.stateVersion(),
                    run.model(),
                    null,
                    run.runtimeConfig()));
            updateFromRuntime(run, resumed);
            transactions.executeWithoutResult(status -> {
                tenantContext.activate(tenantId);
                RunInteraction current = interactions.findForUpdate(
                                tenantId, runId, interactionId)
                        .orElseThrow(this::notFound);
                if (current.status() == RunInteraction.Status.RESPONDING
                        && current.responseRequestId().equals(interaction.responseRequestId())) {
                    interactions.markResponded(current);
                    appendLifecycle(run, "run.interaction.responded", resumed.state(), null, null);
                }
            });
            return requireRun(tenantId, runId, actor);
        } catch (AgentRuntimeException exception) {
            // RESPONDING is intentionally durable. Replaying the same
            // Idempotency-Key retries the same runtime response_request_id.
            throw runtimeFailure(exception);
        }
    }

    public Flow.Publisher<RuntimeEvent> events(
            UUID tenantId,
            UUID runId,
            long lastEventId,
            AuthenticatedActor actor) {
        Run run = requireRun(tenantId, runId, actor);
        Flow.Publisher<RuntimeEvent> source = runtime.subscribeEvents(
                new EventSubscription(command(run), lastEventId));
        return subscriber -> source.subscribe(new Flow.Subscriber<>() {

            @Override
            public void onSubscribe(Flow.Subscription subscription) {
                subscriber.onSubscribe(subscription);
            }

            @Override
            public void onNext(RuntimeEvent event) {
                persistRuntimeEvent(run, event);
                subscriber.onNext(event);
                if (streamEndSignal(event.type())) {
                    refreshAfterStreamSignal(run);
                }
            }

            @Override
            public void onError(Throwable throwable) {
                subscriber.onError(throwable);
            }

            @Override
            public void onComplete() {
                refreshAfterStreamSignal(run);
                subscriber.onComplete();
            }
        });
    }

    public List<RuntimeMessage> history(
            UUID tenantId,
            UUID projectId,
            String threadId,
            AuthenticatedActor actor) {
        RuntimeIdentity identity = transactions.execute(status -> {
            RuntimeIdentity value = requireProject(tenantId, projectId, actor, false);
            if (runs.isConversationRemoved(tenantId, projectId, threadId, actor)) {
                throw conversationNotFound();
            }
            return value;
        });
        if (identity == null) {
            throw notFound();
        }
        try {
            return runtime.getHistory(new HistoryQuery(threadId, identity));
        } catch (AgentRuntimeException exception) {
            throw runtimeFailure(exception);
        }
    }

    public List<ConversationSummary> conversations(
            UUID tenantId,
            UUID projectId,
            AuthenticatedActor actor) {
        List<ConversationSummary> result = transactions.execute(status -> {
            requireProject(tenantId, projectId, actor, false);
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
            requireProject(tenantId, projectId, actor, false);
            Run latest = runs.findLatestForThread(tenantId, projectId, threadId)
                    .orElseThrow(this::conversationNotFound);
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
                status -> requireProject(tenantId, projectId, actor, false));
        if (identity == null) {
            throw notFound();
        }
        try {
            return runtime.getInfo(identity);
        } catch (AgentRuntimeException exception) {
            throw runtimeFailure(exception);
        }
    }

    private Creation createOrGet(
            UUID tenantId,
            UUID projectId,
            String idempotencyKey,
            String threadId,
            StartRequest request,
            Map<String, Object> config,
            CapabilityProfile profile,
            HarnessSelection harness,
            String fingerprint,
            AuthenticatedActor actor) {
        RuntimeIdentity runtimeIdentity = requireProject(tenantId, projectId, actor, true);
        Run existing = runs.findByIdempotency(tenantId, projectId, idempotencyKey).orElse(null);
        if (existing != null) {
            return matching(existing, fingerprint);
        }
        Instant now = Instant.now();
        UUID createdId = UUID.randomUUID();
        Run created = new Run(
                tenantId,
                createdId,
                projectId,
                createdId,
                null,
                1,
                threadId,
                idempotencyKey,
                fingerprint,
                request.message(),
                request.model(),
                config,
                profile.id(),
                profile.version(),
                profile.digest(),
                harness.version().harnessVersionId(),
                harness.version().manifestDigest(),
                harness.channel(),
                runtimeIdentity.principalId(),
                actor.issuer(),
                actor.subject(),
                RunState.QUEUED,
                null,
                null,
                0,
                null,
                null,
                null,
                null,
                now,
                null,
                null);
        runs.insert(created, actor);
        appendLifecycle(created, "run.queued", RunState.QUEUED, null, null);
        return new Creation(created, true);
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
        Run run = runs.findForUpdate(tenantId, runId).orElseThrow(this::notFound);
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
        String fingerprint = interactionFingerprint(interactionId, answer, stateVersion);
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

    private Creation createOrGetRevision(
            UUID tenantId,
            UUID parentRunId,
            String idempotencyKey,
            String feedback,
            AuthenticatedActor actor) {
        MembershipRole role = tenantAccess.requireMembership(tenantId, actor);
        if (!role.canWriteProjects()) {
            throw new ApiException(
                    "RUN_REVISION_DENIED",
                    HttpStatus.FORBIDDEN,
                    "The organization role cannot create run revisions.");
        }
        Run parent = runs.find(tenantId, parentRunId).orElseThrow(this::notFound);
        projects.get(tenantId, parent.projectId(), actor);
        String fingerprint = revisionFingerprint(parent, feedback);
        Run existing = runs.findByIdempotency(tenantId, parent.projectId(), idempotencyKey)
                .orElse(null);
        if (existing != null) {
            return matching(existing, fingerprint);
        }
        if (!terminal(parent.state())) {
            throw new ApiException(
                    "RUN_REVISION_PARENT_ACTIVE",
                    HttpStatus.CONFLICT,
                    "A revision can only be created from a terminal run.");
        }
        runs.findForUpdate(tenantId, parent.rootRunId()).orElseThrow(this::notFound);
        Run latest = runs.findLatestRevision(tenantId, parent.rootRunId()).orElseThrow(this::notFound);
        if (!latest.runId().equals(parent.runId())) {
            throw new ApiException(
                    "RUN_REVISION_STALE_PARENT",
                    HttpStatus.CONFLICT,
                    "The requested parent is not the latest run revision.");
        }
        UUID revisionId = UUID.randomUUID();
        Run revision = new Run(
                tenantId,
                revisionId,
                parent.projectId(),
                parent.rootRunId(),
                parent.runId(),
                runs.nextRevisionNumber(tenantId, parent.rootRunId()),
                parent.threadId(),
                idempotencyKey,
                fingerprint,
                "USER CHANGE REQUEST:\n" + feedback,
                parent.model(),
                parent.runtimeConfig(),
                parent.profileId(),
                parent.profileVersion(),
                parent.profileDigest(),
                parent.harnessVersionId(),
                parent.harnessManifestDigest(),
                parent.harnessChannel(),
                parent.runtimePrincipalId(),
                actor.issuer(),
                actor.subject(),
                RunState.QUEUED,
                null,
                null,
                0,
                null,
                null,
                null,
                null,
                Instant.now(),
                null,
                null);
        runs.insert(revision, actor);
        appendLifecycle(revision, "run.revision.queued", RunState.QUEUED, null, null);
        return new Creation(revision, true);
    }

    private Run existingBeforeRuntime(
            UUID tenantId,
            UUID projectId,
            String idempotencyKey,
            StartRequest request,
            AuthenticatedActor actor) {
        requireProject(tenantId, projectId, actor, true);
        Run existing = runs.findByIdempotency(tenantId, projectId, idempotencyKey).orElse(null);
        if (existing == null) {
            return null;
        }
        ProfileSelector requested = request.capabilityProfile();
        if (existing.profileId() == null
                || !existing.profileId().equals(requested.id())
                || !existing.profileVersion().equals(requested.version())) {
            throw idempotencyConflict();
        }
        String replayFingerprint = fingerprint(
                tenantId,
                projectId,
                request.threadId(),
                request,
                runtimeConfig(
                        request.teamMembers(),
                        existing.profileId(),
                        existing.profileVersion(),
                        existing.profileDigest(),
                        harnessVersions.get(existing.harnessVersionId()),
                        existing.harnessChannel()));
        return matching(existing, replayFingerprint).run();
    }

    private Creation existing(
            UUID tenantId,
            UUID projectId,
            String idempotencyKey,
            String fingerprint,
            AuthenticatedActor actor) {
        requireProject(tenantId, projectId, actor, true);
        Run run = runs.findByIdempotency(tenantId, projectId, idempotencyKey)
                .orElseThrow(() -> new ApiException(
                        "RUN_IDEMPOTENCY_CONFLICT",
                        HttpStatus.CONFLICT,
                        "The idempotency key was used concurrently."));
        return matching(run, fingerprint);
    }

    private Creation matching(Run run, String fingerprint) {
        if (!run.requestFingerprint().equals(fingerprint)) {
            throw idempotencyConflict();
        }
        return new Creation(run, false);
    }

    private ApiException idempotencyConflict() {
        return new ApiException(
                "RUN_IDEMPOTENCY_CONFLICT",
                HttpStatus.CONFLICT,
                "The idempotency key is already associated with different input.");
    }

    private Run requireRun(UUID tenantId, UUID runId, AuthenticatedActor actor) {
        Run result = transactions.execute(status -> {
            tenantAccess.requireMembership(tenantId, actor);
            return runs.find(tenantId, runId).orElseThrow(this::notFound);
        });
        if (result == null) {
            throw notFound();
        }
        return result;
    }

    private Run requireWritableRun(UUID tenantId, UUID runId, AuthenticatedActor actor) {
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

    private RuntimeIdentity requireProject(
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

    private ApiException conversationNotFound() {
        return new ApiException(
                "CONVERSATION_NOT_FOUND",
                HttpStatus.NOT_FOUND,
                "The conversation was not found.");
    }

    private StartRunCommand command(Run run) {
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

    private RunReference reference(Run run) {
        return new RunReference(
                run.runId().toString(),
                run.runtimeIdentity(signer),
                run.harnessChannel());
    }

    private RuntimeIdentity identity(UUID tenantId, UUID projectId, AuthenticatedActor actor) {
        return new RuntimeIdentity(
                signer.principalId(tenantId, projectId, actor),
                tenantId.toString(),
                projectId.toString());
    }

    private CapabilityProfile resolveProfile(
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
        return matches.get(0);
    }

    private void updateFromRuntime(Run run, RuntimeRun runtimeRun) {
        transactions.executeWithoutResult(status -> {
            tenantContext.activate(run.tenantId());
            Run current = runs.findForUpdate(run.tenantId(), run.runId()).orElse(null);
            if (current == null) {
                return;
            }
            boolean stateChanged = runtimeRun.state() != current.state();
            if (runs.updateFromRuntime(run.tenantId(), run.runId(), runtimeRun) && stateChanged) {
                appendLifecycle(
                        current,
                        "run." + runtimeRun.state().name().toLowerCase(java.util.Locale.ROOT),
                        runtimeRun.state(),
                        runtimeRun.errorCode(),
                        runtimeRun.error());
            }
            persistRuntimeResult(current, runtimeRun);
        });
    }

    private void synchronizeTerminalResult(Run run) {
        if (run.deliveryStatus() != null) {
            return;
        }
        try {
            updateFromRuntime(run, runtime.getRun(reference(run)));
        } catch (AgentRuntimeException ignored) {
            // Reconciliation/get may retry; a narrative terminal state cannot
            // fabricate a delivery conclusion without a structured manifest.
        }
    }

    private void markFailed(Run run, String code, String error) {
        transactions.executeWithoutResult(status -> {
            tenantContext.activate(run.tenantId());
            Run current = runs.findForUpdate(run.tenantId(), run.runId()).orElse(null);
            if (current != null && runs.markFailed(run.tenantId(), run.runId(), code, error)) {
                appendLifecycle(current, "run.failed", RunState.FAILED, code, error);
            }
        });
    }

    boolean reconcile(UUID tenantId, UUID runId) {
        Run run = transactions.execute(status -> {
            tenantContext.activate(tenantId);
            return runs.find(tenantId, runId).orElse(null);
        });
        if (run == null || terminal(run.state()) || run.state() == RunState.WAITING_FOR_INPUT) {
            return false;
        }
        // StartRun is idempotent on the stable Java run ID. Calling it for every
        // non-terminal state lets Redis attach to a live lease or fence and take
        // over an expired one; only the lease owner starts a local producer.
        RuntimeRun current = runtime.startRun(command(run));
        updateFromRuntime(run, current);
        return !terminal(current.state());
    }

    private void refreshAfterStreamSignal(Run run) {
        try {
            updateFromRuntime(run, runtime.getRun(reference(run)));
        } catch (AgentRuntimeException ignored) {
            // The bounded reconciliation worker will retry when enabled.
        }
    }

    private void persistRuntimeEvent(Run run, RuntimeEvent event) {
        if (event.eventId() == null || event.eventId() <= 0) {
            return;
        }
        if (evolutionCollector.supports(event)) {
            try {
                // Evolution persistence is an independent transaction. A telemetry
                // failure must never terminate the user-facing Agent event stream.
                evolutionCollector.collect(run, event);
            } catch (RuntimeException exception) {
                logger.warn(
                        "Evolution observation deferred runId={} eventSeq={} cause={}",
                        run.runId(),
                        event.eventId(),
                        exception.getClass().getSimpleName());
            }
        }
        boolean artifactManifest = "artifact_manifest".equals(event.type());
        boolean interactionRequest = "ag_ui".equals(event.type());
        if (!artifactManifest && !interactionRequest
                && (!outboxEnabled || !persistableEvent(event.type()))) {
            return;
        }
        transactions.executeWithoutResult(status -> {
            tenantContext.activate(run.tenantId());
            Run current = runs.findForUpdate(run.tenantId(), run.runId()).orElse(null);
            if (current == null) {
                return;
            }
            if (artifactManifest) {
                persistManifest(current, event.eventId(), event.data());
            }
            if (interactionRequest) {
                persistInteraction(current, event.data());
            }
            if (outboxEnabled && persistableEvent(event.type())) {
                outbox.appendSourceEvent(
                        run.tenantId(),
                        run.runId(),
                        event.eventId(),
                        "run.event." + event.type(),
                        runtimeEventPayload(event));
            }
        });
    }

    private void persistInteraction(Run run, Map<String, Object> event) {
        if (!"CUSTOM".equals(event.get("type"))
                || !"ratsnest.human-input-required.v1".equals(event.get("name"))
                || !(event.get("value") instanceof Map<?, ?> raw)) {
            return;
        }
        Map<String, Object> value = new LinkedHashMap<>();
        raw.forEach((key, item) -> {
            if (key instanceof String text) {
                value.put(text, item);
            }
        });
        String interactionId = stringValue(value.get("interactionId"));
        String kind = stringValue(value.get("kind"));
        String question = stringValue(value.get("question"));
        String requestedBy = stringValue(value.get("requestedBy"));
        Long stateVersion = positiveLong(value.get("stateVersion"));
        if (interactionId == null || !interactionId.matches("[A-Za-z0-9._:-]{1,200}")
                || !"clarification".equals(kind)
                || question == null
                || requestedBy == null
                || !(value.get("options") instanceof List<?>)
                || !(value.get("allowFreeText") instanceof Boolean)
                || stateVersion == null) {
            return;
        }
        boolean created = interactions.register(
                run, interactionId, stateVersion, Map.copyOf(value));
        if (created && runs.markWaitingForInput(run.tenantId(), run.runId())) {
            appendLifecycle(run, "run.waiting_for_input", RunState.WAITING_FOR_INPUT, null, null);
        }
    }

    private void persistRuntimeResult(Run run, RuntimeRun runtimeRun) {
        if (runtimeRun.result().containsKey("artifact_manifest")
                || runtimeRun.result().containsKey("manifest_id")) {
            persistManifest(run, runtimeRun.newestEventId(), runtimeRun.result());
        }
    }

    private void persistManifest(Run run, Long eventId, Map<String, Object> data) {
        ArtifactManifest manifest = artifactManifests.parse(run.runId(), eventId, data);
        boolean inserted = artifacts.persist(run, manifest);
        runs.setDeliveryStatus(run.tenantId(), run.runId(), manifest.deliveryStatus());
        if (inserted && outboxEnabled) {
            Map<String, Object> payload = new LinkedHashMap<>();
            payload.put("requestId", run.runId().toString());
            payload.put("manifestId", manifest.manifestId().toString());
            payload.put("manifestDigest", manifest.digest());
            payload.put("deliveryStatus", manifest.deliveryStatus().apiValue());
            payload.put("artifactCount", manifest.artifacts().size());
            outbox.append(
                    run.tenantId(),
                    run.runId(),
                    "run.delivery." + manifest.deliveryStatus().apiValue(),
                    Map.copyOf(payload));
        }
    }

    private Map<String, Object> runtimeEventPayload(RuntimeEvent event) {
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("sourceEventSeq", event.eventId());
        payload.put("type", event.type());
        if (event.message() != null) {
            payload.put("message", event.message());
        }
        if (event.content() != null) {
            payload.put("content", event.content());
        }
        if (event.error() != null) {
            payload.put("error", event.error());
        }
        if (!event.data().isEmpty()) {
            payload.put("data", event.data());
        }
        return Map.copyOf(payload);
    }

    private void appendLifecycle(
            Run run,
            String eventType,
            RunState state,
            String errorCode,
            String error) {
        if (!outboxEnabled) {
            return;
        }
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("requestId", run.runId().toString());
        payload.put("projectId", run.projectId().toString());
        payload.put("rootRunId", run.rootRunId().toString());
        payload.put("revisionNumber", run.revisionNumber());
        if (run.parentRunId() != null) {
            payload.put("parentRunId", run.parentRunId().toString());
        }
        payload.put("threadId", run.threadId());
        payload.put("state", state.name());
        if (run.profileId() != null) {
            payload.put("profileId", run.profileId());
            payload.put("profileVersion", run.profileVersion());
        }
        payload.put("harnessVersionId", run.harnessVersionId());
        payload.put("harnessManifestDigest", run.harnessManifestDigest());
        payload.put("harnessChannel", run.harnessChannel());
        if (errorCode != null) {
            payload.put("errorCode", errorCode);
        }
        if (error != null) {
            payload.put("error", error);
        }
        outbox.append(run.tenantId(), run.runId(), eventType, Map.copyOf(payload));
    }

    private boolean persistableEvent(String type) {
        return "message".equals(type)
                || "ag_ui".equals(type)
                || "artifact_manifest".equals(type)
                || "error".equals(type)
                || terminalEvent(type);
    }

    private boolean streamEndSignal(String type) {
        return "done".equals(type) || terminalEvent(type);
    }

    private Map<String, Object> runtimeConfig(
            List<TeamMember> teamMembers,
            CapabilityProfile profile,
            HarnessSelection harness) {
        return runtimeConfig(
                teamMembers,
                profile.id(),
                profile.version(),
                profile.digest(),
                harness.version(),
                harness.channel());
    }

    private Map<String, Object> runtimeConfig(
            List<TeamMember> teamMembers,
            String profileId,
            String profileVersion,
            String profileDigest,
            HarnessVersion harness,
            String harnessChannel) {
        List<Map<String, Object>> members = teamMembers.stream()
                .map(member -> Map.<String, Object>of(
                        "role_id", member.roleId(),
                        "name", member.name(),
                        "responsibility", member.responsibility()))
                .toList();
        return Map.of(
                "team_members", members,
                "capability_profile", Map.of(
                        "id", profileId,
                        "version", profileVersion,
                        "digest", profileDigest),
                "harness_version", Map.of(
                        "id", harness.harnessVersionId(),
                        "version", harness.version(),
                        "manifest_digest", harness.manifestDigest(),
                        "source_commit", harness.sourceCommit(),
                        "source_tree_digest", harness.sourceTreeDigest(),
                        "bundle_digest", harness.bundleDigest(),
                        "contract_digest", harness.contractDigest(),
                        "policy_digest", harness.policyDigest(),
                        "channel", harnessChannel));
    }

    private String fingerprint(
            UUID tenantId,
            UUID projectId,
            String threadId,
            StartRequest request,
            Map<String, Object> config) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("tenantId", tenantId);
        value.put("projectId", projectId);
        value.put("threadId", threadId);
        value.put("message", request.message());
        value.put("model", request.model());
        value.put("config", config);
        try {
            return java.util.HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(
                            objectMapper.writeValueAsBytes(canonicalValue(value))));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to fingerprint run request", exception);
        }
    }

    private String revisionFingerprint(Run parent, String feedback) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("tenantId", parent.tenantId());
        value.put("projectId", parent.projectId());
        value.put("parentRunId", parent.runId());
        value.put("feedback", feedback);
        try {
            return java.util.HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(
                            objectMapper.writeValueAsBytes(canonicalValue(value))));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to fingerprint run revision", exception);
        }
    }

    private String interactionFingerprint(String interactionId, String answer, long stateVersion) {
        try {
            return java.util.HexFormat.of().formatHex(
                    MessageDigest.getInstance("SHA-256").digest(
                            objectMapper.writeValueAsBytes(Map.of(
                                    "interactionId", interactionId,
                                    "answer", answer,
                                    "stateVersion", stateVersion))));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to fingerprint interaction response", exception);
        }
    }

    private String stringValue(Object value) {
        return value instanceof String text && !text.isBlank() ? text : null;
    }

    private Long positiveLong(Object value) {
        if (!(value instanceof Number number)) {
            return null;
        }
        long result = number.longValue();
        return result > 0 ? result : null;
    }

    private Object canonicalValue(Object value) {
        if (value instanceof Map<?, ?> map) {
            Map<String, Object> sorted = new TreeMap<>();
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!(entry.getKey() instanceof String key)) {
                    throw new IllegalArgumentException("Run request keys must be strings");
                }
                sorted.put(key, canonicalValue(entry.getValue()));
            }
            return sorted;
        }
        if (value instanceof List<?> list) {
            return list.stream().map(this::canonicalValue).toList();
        }
        return value;
    }

    private ApiException runtimeFailure(AgentRuntimeException exception) {
        HttpStatus status = exception.status() == 404
                ? HttpStatus.NOT_FOUND
                : exception.status() == 409
                        ? HttpStatus.CONFLICT
                        : HttpStatus.BAD_GATEWAY;
        return new ApiException("AGENT_RUNTIME_ERROR", status, exception.getMessage());
    }

    private ApiException notFound() {
        return new ApiException("RUN_NOT_FOUND", HttpStatus.NOT_FOUND, "The run was not found.");
    }

    private boolean terminal(RunState state) {
        return state == RunState.COMPLETED
                || state == RunState.FAILED
                || state == RunState.CANCELLED
                || state == RunState.TIMED_OUT;
    }

    private boolean terminalEvent(String type) {
        return "completed".equals(type)
                || "failed".equals(type)
                || "cancelled".equals(type)
                || "timed_out".equals(type);
    }

    public record StartRequest(
            String message,
            String model,
            String threadId,
            ProfileSelector capabilityProfile,
            List<TeamMember> teamMembers) {

        public StartRequest {
            teamMembers = teamMembers == null ? List.of() : List.copyOf(teamMembers);
        }
    }

    public record ProfileSelector(String id, String version) {
    }

    private record InteractionResponse(Run run, RunInteraction interaction, boolean dispatch) {
    }

    public record TeamMember(String roleId, String name, String responsibility) {
    }

    private record Creation(Run run, boolean created) {
    }
}
