package team.ratsnest.controlplane.run.api;

import static team.ratsnest.controlplane.shared.web.ApiHeaders.ORGANIZATION_HEADER;

import java.io.IOException;
import java.net.URI;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.Flow;

import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.CapabilityProfile;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeInfo;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeMessage;
import team.ratsnest.controlplane.identity.api.JwtIdentity;
import team.ratsnest.controlplane.run.application.RunInteractionService;
import team.ratsnest.controlplane.run.application.RunLifecycleService;
import team.ratsnest.controlplane.run.application.RunQueryService;
import team.ratsnest.controlplane.run.application.RunSubmissionService;
import team.ratsnest.controlplane.run.application.model.ForkReplayMode;
import team.ratsnest.controlplane.run.application.model.ForkRequest;
import team.ratsnest.controlplane.run.application.model.ProfileSelector;
import team.ratsnest.controlplane.run.application.model.RunRuntimeStatus;
import team.ratsnest.controlplane.run.application.model.RunRuntimeStatus.RunActivitySnapshot;
import team.ratsnest.controlplane.run.application.model.StartRequest;
import team.ratsnest.controlplane.run.application.model.TeamMember;
import team.ratsnest.controlplane.run.domain.model.ConversationSummary;
import team.ratsnest.controlplane.run.domain.model.Run;
import team.ratsnest.controlplane.shared.web.ApiException;

@RestController
@Validated
@RequestMapping("/api/v1")
public class RunController {

    private static final String IDEMPOTENCY_HEADER = "Idempotency-Key";
    private static final String PROFILE_ID_PATTERN = "[a-z0-9][a-z0-9-]{1,63}";
    private static final String PROFILE_VERSION_PATTERN =
            "(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)(?:\\.(?:0|[1-9][0-9]*))?";

    private final RunSubmissionService submissions;
    private final RunQueryService queries;
    private final RunInteractionService interactions;
    private final RunLifecycleService lifecycle;

    public RunController(
            RunSubmissionService submissions,
            RunQueryService queries,
            RunInteractionService interactions,
            RunLifecycleService lifecycle) {
        this.submissions = submissions;
        this.queries = queries;
        this.interactions = interactions;
        this.lifecycle = lifecycle;
    }

    @PostMapping("/projects/{projectId}/runs")
    ResponseEntity<RunResponse> start(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @RequestHeader(IDEMPOTENCY_HEADER)
            @Pattern(regexp = "[A-Za-z0-9._:-]{8,200}") String idempotencyKey,
            @Valid @RequestBody StartRunRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        Run run = submissions.start(
                tenantId,
                projectId,
                idempotencyKey,
                request.toServiceRequest(),
                JwtIdentity.from(jwt));
        return ResponseEntity.accepted()
                .location(URI.create("/api/v1/runs/" + run.runId()))
                .body(RunResponse.from(run));
    }

    @GetMapping("/runs/{runId}")
    RunResponse get(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @AuthenticationPrincipal Jwt jwt) {
        return RunResponse.from(queries.get(tenantId, runId, JwtIdentity.from(jwt)));
    }

    @PostMapping("/runs/{sourceRunId}/forks")
    ResponseEntity<RunResponse> fork(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID sourceRunId,
            @RequestHeader(IDEMPOTENCY_HEADER)
            @Pattern(regexp = "[A-Za-z0-9._:-]{8,200}") String idempotencyKey,
            @Valid @RequestBody ForkRunRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        Run fork = submissions.fork(
                tenantId,
                sourceRunId,
                idempotencyKey,
                request.toServiceRequest(),
                JwtIdentity.from(jwt));
        return ResponseEntity.accepted()
                .location(URI.create("/api/v1/runs/" + fork.runId()))
                .body(RunResponse.from(fork));
    }

    @GetMapping("/runs/{runId}/runtime-status")
    RuntimeStatusResponse runtimeStatus(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @AuthenticationPrincipal Jwt jwt) {
        return RuntimeStatusResponse.from(
                queries.runtimeStatus(tenantId, runId, JwtIdentity.from(jwt)));
    }

    @PostMapping("/runs/{runId}:recover")
    ResponseEntity<RuntimeStatusResponse> recover(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @RequestHeader(IDEMPOTENCY_HEADER)
            @Pattern(regexp = "[A-Za-z0-9._:-]{8,200}") String idempotencyKey,
            @AuthenticationPrincipal Jwt jwt) {
        RunRuntimeStatus status = interactions.recover(
                tenantId, runId, JwtIdentity.from(jwt));
        return ResponseEntity.accepted().body(RuntimeStatusResponse.from(status));
    }

    @PostMapping("/runs/{runId}:cancel")
    RunResponse cancel(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @AuthenticationPrincipal Jwt jwt) {
        return RunResponse.from(lifecycle.cancel(tenantId, runId, JwtIdentity.from(jwt)));
    }

    @PostMapping("/runs/{runId}/interactions/{interactionId}:respond")
    ResponseEntity<RunResponse> respond(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @PathVariable @Pattern(regexp = "[A-Za-z0-9._:-]{1,200}") String interactionId,
            @RequestHeader(IDEMPOTENCY_HEADER)
            @Pattern(regexp = "[A-Za-z0-9._:-]{8,200}") String idempotencyKey,
            @Valid @RequestBody InteractionResponseRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        Run run = interactions.respond(
                tenantId,
                runId,
                interactionId,
                idempotencyKey,
                request.answer().strip(),
                request.stateVersion(),
                JwtIdentity.from(jwt));
        return ResponseEntity.accepted().body(RunResponse.from(run));
    }

    @PostMapping("/runs/{runId}/revisions")
    ResponseEntity<RunResponse> revise(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @RequestHeader(IDEMPOTENCY_HEADER)
            @Pattern(regexp = "[A-Za-z0-9._:-]{8,200}") String idempotencyKey,
            @Valid @RequestBody RevisionRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        Run revision = submissions.revise(
                tenantId,
                runId,
                idempotencyKey,
                request.feedback().strip(),
                JwtIdentity.from(jwt));
        return ResponseEntity.accepted()
                .location(URI.create("/api/v1/runs/" + revision.runId()))
                .body(RunResponse.from(revision));
    }

    @GetMapping(value = "/runs/{runId}/events", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    SseEmitter events(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @RequestHeader(name = "Last-Event-ID", required = false) String cursor,
            @AuthenticationPrincipal Jwt jwt) {
        long lastEventId = parseCursor(cursor);
        SseEmitter emitter = new SseEmitter(0L);
        lifecycle.events(tenantId, runId, lastEventId, JwtIdentity.from(jwt))
                .subscribe(new SseSubscriber(emitter, runId));
        return emitter;
    }

    @GetMapping("/projects/{projectId}/threads/{threadId}/messages")
    HistoryResponse history(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @PathVariable @Pattern(regexp = "[A-Za-z0-9._:-]{1,200}") String threadId,
            @AuthenticationPrincipal Jwt jwt) {
        List<MessageResponse> messages = queries.history(
                        tenantId,
                        projectId,
                        threadId,
                        JwtIdentity.from(jwt))
                .stream()
                .map(MessageResponse::from)
                .toList();
        return new HistoryResponse(messages);
    }

    @GetMapping("/projects/{projectId}/threads")
    ConversationListResponse conversations(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @AuthenticationPrincipal Jwt jwt) {
        return new ConversationListResponse(queries.conversations(
                        tenantId,
                        projectId,
                        JwtIdentity.from(jwt))
                .stream()
                .map(ConversationResponse::from)
                .toList());
    }

    @DeleteMapping("/projects/{projectId}/threads/{threadId}")
    ResponseEntity<Void> removeConversation(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @PathVariable @Pattern(regexp = "[A-Za-z0-9._:-]{1,200}") String threadId,
            @AuthenticationPrincipal Jwt jwt) {
        queries.removeConversation(
                tenantId,
                projectId,
                threadId,
                JwtIdentity.from(jwt));
        return ResponseEntity.noContent().build();
    }

    @GetMapping("/projects/{projectId}/runtime-info")
    RuntimeInfoResponse info(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @AuthenticationPrincipal Jwt jwt) {
        return RuntimeInfoResponse.from(
                queries.info(tenantId, projectId, JwtIdentity.from(jwt)));
    }

    private long parseCursor(String value) {
        if (value == null || value.isBlank()) {
            return 0;
        }
        if (!value.matches("\\d{1,19}")) {
            throw new ApiException(
                    "INVALID_EVENT_CURSOR",
                    HttpStatus.BAD_REQUEST,
                    "Last-Event-ID must be a non-negative integer.");
        }
        try {
            return Long.parseLong(value);
        } catch (NumberFormatException exception) {
            throw new ApiException(
                    "INVALID_EVENT_CURSOR",
                    HttpStatus.BAD_REQUEST,
                    "Last-Event-ID is outside the supported range.");
        }
    }

    record StartRunRequest(
            @NotBlank @Size(max = 100_000) String message,
            @Size(max = 200) String model,
            @Pattern(regexp = "[A-Za-z0-9._:-]{1,200}") String threadId,
            @NotNull @Valid CapabilityProfileRequest capabilityProfile,
            @Size(max = 8) List<@Valid TeamMemberRequest> teamMembers,
            @Pattern(regexp = "ratsnestpro-(?:multi-agent|single-agent-eval)")
                    String agentId,
            @Valid EvaluationContextRequest evaluationContext) {

        StartRequest toServiceRequest() {
            List<TeamMember> members = teamMembers == null
                    ? List.of()
                    : teamMembers.stream().map(TeamMemberRequest::toService).toList();
            return new StartRequest(
                    message.strip(),
                    model,
                    threadId,
                    capabilityProfile.toService(),
                    members,
                    agentId,
                    evaluationContext == null ? Map.of() : evaluationContext.toService());
        }
    }

    record EvaluationContextRequest(
            @NotBlank @Pattern(regexp = "[A-Za-z0-9._:-]{1,200}") String planId,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String planDigest,
            @NotBlank @Pattern(regexp = "[A-Za-z0-9._:-]{1,200}") String pairId,
            @NotBlank @Pattern(regexp = "[A-Za-z0-9._:-]{1,200}") String caseId,
            @NotBlank @Pattern(regexp = "single_agent|multi_agent") String arm,
            @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String promptDigest) {

        Map<String, String> toService() {
            return Map.of(
                    "plan_id", planId,
                    "plan_digest", planDigest,
                    "pair_id", pairId,
                    "case_id", caseId,
                    "arm", arm,
                    "prompt_digest", promptDigest);
        }
    }

    record CapabilityProfileRequest(
            @NotBlank @Pattern(regexp = PROFILE_ID_PATTERN) String id,
            @NotBlank @Pattern(regexp = PROFILE_VERSION_PATTERN) String version) {

        ProfileSelector toService() {
            return new ProfileSelector(id, version);
        }
    }

    record ForkRunRequest(
            @NotNull @Valid CapabilityProfileRequest capabilityProfile,
            @NotNull ForkReplayMode replayMode,
            @Size(max = 100_000) @Pattern(regexp = "(?s).*\\S.*") String changeRequest,
            @Size(max = 200) String model,
            @Size(max = 8) List<@Valid TeamMemberRequest> teamMembers) {

        ForkRequest toServiceRequest() {
            List<TeamMember> members = teamMembers == null
                    ? List.of()
                    : teamMembers.stream().map(TeamMemberRequest::toService).toList();
            return new ForkRequest(
                    capabilityProfile.toService(),
                    replayMode,
                    changeRequest,
                    model,
                    members);
        }
    }

    record TeamMemberRequest(
            @NotBlank @Pattern(regexp = "[a-z0-9][a-z0-9-]{1,63}") String roleId,
            @NotBlank @Size(max = 80) String name,
            @NotBlank @Size(max = 500) String responsibility) {

        TeamMember toService() {
            return new TeamMember(roleId, name.strip(), responsibility.strip());
        }
    }

    record RevisionRequest(@NotBlank @Size(max = 99_979) String feedback) {
    }

    record InteractionResponseRequest(
            @NotBlank @Size(max = 100_000) String answer,
            @Positive long stateVersion) {
    }

    record RunResponse(
            UUID runId,
            UUID projectId,
            UUID rootRunId,
            UUID parentRunId,
            UUID forkedFromRunId,
            int revisionNumber,
            String threadId,
            CapabilityProfileSnapshot capabilityProfile,
            HarnessVersionSnapshot harnessVersion,
            String state,
            String deliveryStatus,
            Instant createdAt,
            Instant startedAt,
            Instant finishedAt,
            long eventCount,
            String errorCode,
            String error) {

        static RunResponse from(Run run) {
            return new RunResponse(
                    run.runId(),
                    run.projectId(),
                    run.rootRunId(),
                    run.parentRunId(),
                    run.forkedFromRunId(),
                    run.revisionNumber(),
                    run.threadId(),
                    run.profileId() == null
                            ? null
                            : new CapabilityProfileSnapshot(
                                    run.profileId(),
                                    run.profileVersion(),
                                    run.profileDigest()),
                    new HarnessVersionSnapshot(
                            run.harnessVersionId(),
                            run.harnessManifestDigest(),
                            run.harnessChannel()),
                    run.state().name(),
                    run.deliveryStatus() == null ? null : run.deliveryStatus().apiValue(),
                    run.createdAt(),
                    run.startedAt(),
                    run.finishedAt(),
                    run.eventCount(),
                    run.errorCode(),
                    run.error());
        }
    }

    record RuntimeStatusResponse(
            UUID runId,
            String controlState,
            String runtimeState,
            String recoveryState,
            Boolean leaseActive,
            Boolean recoverable,
            Instant leaseExpiresAt,
            long lastEventId,
            long eventCount,
            Instant checkedAt,
            RunActivitySnapshot activity) {

        static RuntimeStatusResponse from(RunRuntimeStatus status) {
            return new RuntimeStatusResponse(
                    status.runId(),
                    status.controlState().name(),
                    status.runtimeState() == null ? "UNKNOWN" : status.runtimeState().name(),
                    status.executionStatus().name(),
                    status.leaseActive(),
                    status.recoverable(),
                    status.leaseExpiresAt(),
                    status.lastEventId(),
                    status.eventCount(),
                    status.checkedAt(),
                    status.activity());
        }
    }

    record CapabilityProfileSnapshot(String id, String version, String digest) {
    }

    record HarnessVersionSnapshot(String id, String manifestDigest, String channel) {
    }

    record RunEventResponse(
            long eventId,
            UUID runId,
            String type,
            Instant createdAt,
            Map<String, Object> data) {
    }

    record MessageResponse(
            String type,
            String content,
            List<Map<String, Object>> toolCalls,
            String toolCallId,
            String runId,
            Map<String, Object> responseMetadata,
            Map<String, Object> customData) {

        static MessageResponse from(RuntimeMessage message) {
            return new MessageResponse(
                    message.type(),
                    message.content(),
                    message.toolCalls(),
                    message.toolCallId(),
                    message.runId(),
                    message.responseMetadata(),
                    message.customData());
        }
    }

    record HistoryResponse(List<MessageResponse> messages) {
    }

    record ConversationResponse(
            String threadId,
            String title,
            UUID latestRunId,
            int latestRevisionNumber,
            String state,
            String deliveryStatus,
            long lastEventId,
            Map<String, Object> pendingInteraction,
            Instant createdAt,
            Instant updatedAt) {

        static ConversationResponse from(ConversationSummary conversation) {
            return new ConversationResponse(
                    conversation.threadId(),
                    conversation.title(),
                    conversation.latestRunId(),
                    conversation.latestRevisionNumber(),
                    conversation.state().name(),
                    conversation.deliveryStatus() == null
                            ? null
                            : conversation.deliveryStatus().apiValue(),
                    conversation.lastEventId(),
                    conversation.pendingInteraction().isEmpty()
                            ? null
                            : conversation.pendingInteraction(),
                    conversation.createdAt(),
                    conversation.updatedAt());
        }
    }

    record ConversationListResponse(List<ConversationResponse> conversations) {
    }

    record RuntimeInfoResponse(
            List<Map<String, String>> agents,
            List<String> models,
            String defaultAgent,
            String defaultModel,
            List<CapabilityProfileMetadata> profiles) {

        static RuntimeInfoResponse from(RuntimeInfo info) {
            return new RuntimeInfoResponse(
                    info.agents(),
                    info.models(),
                    info.defaultAgent(),
                    info.defaultModel(),
                    info.profiles().stream().map(CapabilityProfileMetadata::from).toList());
        }
    }

    record CapabilityProfileMetadata(
            String id,
            String version,
            String digest,
            String title,
            String description) {

        static CapabilityProfileMetadata from(CapabilityProfile profile) {
            return new CapabilityProfileMetadata(
                    profile.id(),
                    profile.version(),
                    profile.digest(),
                    profile.title(),
                    profile.description());
        }
    }

    private static final class SseSubscriber implements Flow.Subscriber<RuntimeEvent> {

        private final SseEmitter emitter;
        private final UUID runId;
        private Flow.Subscription subscription;

        private SseSubscriber(SseEmitter emitter, UUID runId) {
            this.emitter = emitter;
            this.runId = runId;
        }

        @Override
        public void onSubscribe(Flow.Subscription subscription) {
            this.subscription = subscription;
            emitter.onCompletion(subscription::cancel);
            emitter.onTimeout(subscription::cancel);
            emitter.onError(error -> subscription.cancel());
            subscription.request(Long.MAX_VALUE);
        }

        @Override
        public void onNext(RuntimeEvent event) {
            try {
                if ("heartbeat".equals(event.type())) {
                    emitter.send(SseEmitter.event().comment("heartbeat"));
                    return;
                }
                if (event.eventId() == null) {
                    throw new IOException("Agent Runtime event is missing an event ID");
                }
                RunEventResponse response = new RunEventResponse(
                        event.eventId(),
                        runId,
                        event.type(),
                        Instant.now(),
                        eventData(event));
                emitter.send(SseEmitter.event()
                        .id(Long.toString(event.eventId()))
                        .name(event.type())
                        .data(response, MediaType.APPLICATION_JSON));
            } catch (IOException exception) {
                subscription.cancel();
                emitter.completeWithError(exception);
            }
        }

        @Override
        public void onError(Throwable throwable) {
            emitter.completeWithError(throwable);
        }

        @Override
        public void onComplete() {
            emitter.complete();
        }

        private Map<String, Object> eventData(RuntimeEvent event) {
            Map<String, Object> data = new LinkedHashMap<>(event.data());
            if (event.message() != null) {
                data.put("message", MessageResponse.from(event.message()));
            }
            if (event.content() != null) {
                data.put("content", event.content());
            }
            if (event.error() != null) {
                data.put("error", event.error());
            }
            return Map.copyOf(data);
        }
    }
}
