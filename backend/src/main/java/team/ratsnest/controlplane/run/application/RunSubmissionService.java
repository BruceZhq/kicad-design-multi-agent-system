package team.ratsnest.controlplane.run.application;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;

import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.CapabilityProfile;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeIdentity;
import team.ratsnest.controlplane.harness.application.HarnessReleaseRouter;
import team.ratsnest.controlplane.harness.application.HarnessReleaseRouter.HarnessSelection;
import team.ratsnest.controlplane.harness.application.HarnessVersionService;
import team.ratsnest.controlplane.identity.domain.model.AuthenticatedActor;
import team.ratsnest.controlplane.project.application.ProjectService;
import team.ratsnest.controlplane.run.application.model.ForkRequest;
import team.ratsnest.controlplane.run.application.model.ProfileSelector;
import team.ratsnest.controlplane.run.application.model.StartRequest;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.run.domain.port.RunStore;
import team.ratsnest.controlplane.shared.web.ApiException;
import team.ratsnest.controlplane.tenancy.application.TenantAccess;
import team.ratsnest.controlplane.tenancy.domain.model.MembershipRole;

/** Root-run, revision and replay-fork submission use cases. */
@Service
public class RunSubmissionService {

    private final TransactionTemplate transactions;
    private final TenantAccess tenantAccess;
    private final ProjectService projects;
    private final RunStore runs;
    private final HarnessVersionService harnessVersions;
    private final HarnessReleaseRouter harnessReleaseRouter;
    private final RunAccessSupport access;
    private final RunRuntimeConfiguration runtimeConfiguration;
    private final RunRequestFingerprint fingerprints;
    private final RunLifecycleService lifecycle;

    public RunSubmissionService(
            TransactionTemplate transactions,
            TenantAccess tenantAccess,
            ProjectService projects,
            RunStore runs,
            HarnessVersionService harnessVersions,
            HarnessReleaseRouter harnessReleaseRouter,
            RunAccessSupport access,
            RunRuntimeConfiguration runtimeConfiguration,
            RunRequestFingerprint fingerprints,
            RunLifecycleService lifecycle) {
        this.transactions = transactions;
        this.tenantAccess = tenantAccess;
        this.projects = projects;
        this.runs = runs;
        this.harnessVersions = harnessVersions;
        this.harnessReleaseRouter = harnessReleaseRouter;
        this.access = access;
        this.runtimeConfiguration = runtimeConfiguration;
        this.fingerprints = fingerprints;
        this.lifecycle = lifecycle;
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
        CapabilityProfile profile = access.resolveProfile(
                tenantId,
                projectId,
                request.capabilityProfile(),
                actor);
        HarnessSelection harness = harnessReleaseRouter.route(
                tenantId, projectId, idempotencyKey);
        Map<String, Object> config = runtimeConfiguration.create(
                request.teamMembers(), profile, harness);
        String fingerprint = fingerprints.start(
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
        return lifecycle.dispatchStart(creation.run(), actor);
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
            Run parent = access.requireWritableRun(tenantId, runId, actor);
            String fingerprint = fingerprints.revision(parent, feedback);
            creation = transactions.execute(status -> existing(
                    tenantId, parent.projectId(), idempotencyKey, fingerprint, actor));
        }
        if (creation == null) {
            throw new IllegalStateException("Run revision transaction returned no result");
        }
        if (!creation.created()) {
            return creation.run();
        }
        return lifecycle.dispatchStart(creation.run(), actor);
    }

    public Run fork(
            UUID tenantId,
            UUID sourceRunId,
            String idempotencyKey,
            ForkRequest request,
            AuthenticatedActor actor) {
        Run replay = transactions.execute(status -> existingForkBeforeRuntime(
                tenantId, sourceRunId, idempotencyKey, request, actor));
        if (replay != null) {
            return replay;
        }

        ForkSource initial = transactions.execute(
                status -> loadForkSource(tenantId, sourceRunId, actor));
        if (initial == null) {
            throw new IllegalStateException("Run fork source transaction returned no result");
        }
        CapabilityProfile profile = access.resolveProfile(
                tenantId,
                initial.source().projectId(),
                request.capabilityProfile(),
                actor);
        HarnessSelection harness = harnessReleaseRouter.route(
                tenantId, initial.source().projectId(), idempotencyKey);
        Map<String, Object> config = runtimeConfiguration.create(
                request.teamMembers(), profile, harness);

        Creation creation;
        try {
            creation = transactions.execute(status -> createOrGetFork(
                    tenantId,
                    sourceRunId,
                    idempotencyKey,
                    request,
                    config,
                    profile,
                    harness,
                    actor));
        } catch (DataIntegrityViolationException exception) {
            ForkSource source = transactions.execute(
                    status -> loadForkSource(tenantId, sourceRunId, actor));
            if (source == null) {
                throw new IllegalStateException("Run fork source transaction returned no result");
            }
            String fingerprint = forkFingerprint(
                    source,
                    request,
                    effectiveForkModel(source.source(), request),
                    config);
            creation = transactions.execute(status -> existing(
                    tenantId,
                    source.source().projectId(),
                    idempotencyKey,
                    fingerprint,
                    actor));
        }
        if (creation == null) {
            throw new IllegalStateException("Run fork transaction returned no result");
        }
        if (!creation.created()) {
            return creation.run();
        }
        return lifecycle.dispatchStart(creation.run(), actor);
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
        RuntimeIdentity runtimeIdentity = access.requireProject(tenantId, projectId, actor, true);
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
        lifecycle.appendLifecycle(created, "run.queued", RunState.QUEUED, null, null);
        return new Creation(created, true);
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
        Run parent = runs.find(tenantId, parentRunId).orElseThrow(access::notFound);
        projects.get(tenantId, parent.projectId(), actor);
        String fingerprint = fingerprints.revision(parent, feedback);
        Run existing = runs.findByIdempotency(tenantId, parent.projectId(), idempotencyKey)
                .orElse(null);
        if (existing != null) {
            return matching(existing, fingerprint);
        }
        if (!RunAccessSupport.terminal(parent.state())) {
            throw new ApiException(
                    "RUN_REVISION_PARENT_ACTIVE",
                    HttpStatus.CONFLICT,
                    "A revision can only be created from a terminal run.");
        }
        runs.findForUpdate(tenantId, parent.rootRunId()).orElseThrow(access::notFound);
        Run latest = runs.findLatestRevision(tenantId, parent.rootRunId())
                .orElseThrow(access::notFound);
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
        lifecycle.appendLifecycle(
                revision, "run.revision.queued", RunState.QUEUED, null, null);
        return new Creation(revision, true);
    }

    private Creation createOrGetFork(
            UUID tenantId,
            UUID sourceRunId,
            String idempotencyKey,
            ForkRequest request,
            Map<String, Object> config,
            CapabilityProfile profile,
            HarnessSelection harness,
            AuthenticatedActor actor) {
        ForkSource source = loadForkSource(tenantId, sourceRunId, actor);
        String model = effectiveForkModel(source.source(), request);
        String fingerprint = forkFingerprint(source, request, model, config);
        Run existing = runs.findByIdempotency(
                        tenantId, source.source().projectId(), idempotencyKey)
                .orElse(null);
        if (existing != null) {
            return matching(existing, fingerprint);
        }

        RuntimeIdentity runtimeIdentity = access.requireProject(
                tenantId, source.source().projectId(), actor, true);
        UUID forkId = UUID.randomUUID();
        Run fork = new Run(
                tenantId,
                forkId,
                source.source().projectId(),
                forkId,
                null,
                source.source().runId(),
                1,
                UUID.randomUUID().toString(),
                idempotencyKey,
                fingerprint,
                forkMessage(source, request.changeRequest()),
                model,
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
                Instant.now(),
                null,
                null);
        runs.insert(fork, actor);
        lifecycle.appendLifecycle(fork, "run.fork.queued", RunState.QUEUED, null, null);
        return new Creation(fork, true);
    }

    private Run existingForkBeforeRuntime(
            UUID tenantId,
            UUID sourceRunId,
            String idempotencyKey,
            ForkRequest request,
            AuthenticatedActor actor) {
        ForkSource source = loadForkSource(tenantId, sourceRunId, actor);
        Run existing = runs.findByIdempotency(
                        tenantId, source.source().projectId(), idempotencyKey)
                .orElse(null);
        if (existing == null) {
            return null;
        }
        if (!Objects.equals(existing.forkedFromRunId(), sourceRunId)
                || existing.profileId() == null
                || !existing.profileId().equals(request.capabilityProfile().id())
                || !existing.profileVersion().equals(request.capabilityProfile().version())) {
            throw idempotencyConflict();
        }
        Map<String, Object> config = runtimeConfiguration.create(
                request.teamMembers(),
                existing.profileId(),
                existing.profileVersion(),
                existing.profileDigest(),
                harnessVersions.get(existing.harnessVersionId()),
                existing.harnessChannel());
        String fingerprint = forkFingerprint(
                source,
                request,
                effectiveForkModel(source.source(), request),
                config);
        return matching(existing, fingerprint).run();
    }

    private ForkSource loadForkSource(
            UUID tenantId,
            UUID sourceRunId,
            AuthenticatedActor actor) {
        MembershipRole role = tenantAccess.requireMembership(tenantId, actor);
        if (!role.canWriteProjects()) {
            throw new ApiException(
                    "RUN_FORK_DENIED",
                    HttpStatus.FORBIDDEN,
                    "The organization role cannot fork runs.");
        }
        Run source = runs.find(tenantId, sourceRunId).orElseThrow(access::notFound);
        projects.get(tenantId, source.projectId(), actor);
        if (!RunAccessSupport.terminal(source.state())) {
            throw new ApiException(
                    "RUN_FORK_SOURCE_ACTIVE",
                    HttpStatus.CONFLICT,
                    "A run fork can only be created from a terminal run.");
        }

        List<Run> chain = runs.findRevisionChainThrough(
                tenantId, source.rootRunId(), source.revisionNumber());
        if (chain.size() != source.revisionNumber()
                || chain.isEmpty()
                || !chain.getFirst().runId().equals(source.rootRunId())
                || !chain.getLast().runId().equals(source.runId())) {
            throw invalidForkSource();
        }
        for (int index = 0; index < chain.size(); index++) {
            Run item = chain.get(index);
            if (item.revisionNumber() != index + 1
                    || !item.rootRunId().equals(source.rootRunId())
                    || !item.projectId().equals(source.projectId())
                    || (index == 0 && item.parentRunId() != null)
                    || (index > 0 && !Objects.equals(
                            item.parentRunId(), chain.get(index - 1).runId()))
                    || (index > 0 && !item.message().startsWith("USER CHANGE REQUEST:\n"))) {
                throw invalidForkSource();
            }
        }

        String baseMessage = chain.stream()
                .map(Run::message)
                .collect(java.util.stream.Collectors.joining("\n\n"));
        List<Map<String, Object>> digestChain = chain.stream()
                .map(item -> Map.<String, Object>of(
                        "runId", item.runId(),
                        "revisionNumber", item.revisionNumber(),
                        "message", item.message()))
                .toList();
        String chainDigest = fingerprints.digest(
                Map.of(
                        "rootRunId", source.rootRunId(),
                        "sourceRunId", source.runId(),
                        "revisions", digestChain),
                "Unable to fingerprint run fork source");
        return new ForkSource(source, baseMessage, chainDigest);
    }

    private String forkMessage(ForkSource source, String changeRequest) {
        String message = source.baseMessage();
        if (changeRequest != null && !changeRequest.isBlank()) {
            message += "\n\nUSER CHANGE REQUEST:\n" + changeRequest;
        }
        if (message.length() > 100_000) {
            throw new ApiException(
                    "RUN_FORK_MESSAGE_TOO_LARGE",
                    HttpStatus.UNPROCESSABLE_ENTITY,
                    "The reconstructed run request exceeds 100000 characters.");
        }
        return message;
    }

    private String effectiveForkModel(Run source, ForkRequest request) {
        return request.model() == null ? source.model() : request.model();
    }

    private String forkFingerprint(
            ForkSource source,
            ForkRequest request,
            String model,
            Map<String, Object> config) {
        Map<String, Object> value = new LinkedHashMap<>();
        value.put("tenantId", source.source().tenantId());
        value.put("projectId", source.source().projectId());
        value.put("forkedFromRunId", source.source().runId());
        value.put("sourceChainDigest", source.chainDigest());
        value.put("replayMode", request.replayMode().name());
        value.put("changeRequest", request.changeRequest());
        value.put("message", forkMessage(source, request.changeRequest()));
        value.put("model", model);
        value.put("config", config);
        return fingerprints.digest(value, "Unable to fingerprint run fork request");
    }

    private Run existingBeforeRuntime(
            UUID tenantId,
            UUID projectId,
            String idempotencyKey,
            StartRequest request,
            AuthenticatedActor actor) {
        access.requireProject(tenantId, projectId, actor, true);
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
        String replayFingerprint = fingerprints.start(
                tenantId,
                projectId,
                request.threadId(),
                request,
                runtimeConfiguration.create(
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
        access.requireProject(tenantId, projectId, actor, true);
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

    private ApiException invalidForkSource() {
        return new ApiException(
                "RUN_FORK_SOURCE_INVALID",
                HttpStatus.CONFLICT,
                "The source revision chain is incomplete or invalid.");
    }

    private record Creation(Run run, boolean created) {
    }

    private record ForkSource(Run source, String baseMessage, String chainDigest) {
    }
}
