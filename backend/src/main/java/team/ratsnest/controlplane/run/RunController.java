package team.ratsnest.controlplane.run;

import static team.ratsnest.controlplane.organization.OrganizationController.ORGANIZATION_HEADER;

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
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.CapabilityProfile;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeEvent;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeInfo;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.RuntimeMessage;
import team.ratsnest.controlplane.identity.AuthenticatedActor;
import team.ratsnest.controlplane.run.RunService.StartRequest;
import team.ratsnest.controlplane.run.RunService.ProfileSelector;
import team.ratsnest.controlplane.run.RunService.TeamMember;
import team.ratsnest.controlplane.shared.web.ApiException;

@RestController
@Validated
@RequestMapping("/api/v1")
public class RunController {

    private static final String IDEMPOTENCY_HEADER = "Idempotency-Key";
    private static final String PROFILE_ID_PATTERN = "[a-z0-9][a-z0-9-]{1,63}";
    private static final String PROFILE_VERSION_PATTERN =
            "(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)(?:\\.(?:0|[1-9][0-9]*))?";

    private final RunService runs;

    public RunController(RunService runs) {
        this.runs = runs;
    }

    @PostMapping("/projects/{projectId}/runs")
    ResponseEntity<RunResponse> start(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @RequestHeader(IDEMPOTENCY_HEADER)
            @Pattern(regexp = "[A-Za-z0-9._:-]{8,200}") String idempotencyKey,
            @Valid @RequestBody StartRunRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        Run run = runs.start(
                tenantId,
                projectId,
                idempotencyKey,
                request.toServiceRequest(),
                AuthenticatedActor.from(jwt));
        return ResponseEntity.accepted()
                .location(URI.create("/api/v1/runs/" + run.runId()))
                .body(RunResponse.from(run));
    }

    @GetMapping("/runs/{runId}")
    RunResponse get(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @AuthenticationPrincipal Jwt jwt) {
        return RunResponse.from(runs.get(tenantId, runId, AuthenticatedActor.from(jwt)));
    }

    @PostMapping("/runs/{runId}:cancel")
    RunResponse cancel(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID runId,
            @AuthenticationPrincipal Jwt jwt) {
        return RunResponse.from(runs.cancel(tenantId, runId, AuthenticatedActor.from(jwt)));
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
        Run run = runs.respond(
                tenantId,
                runId,
                interactionId,
                idempotencyKey,
                request.answer().strip(),
                request.stateVersion(),
                AuthenticatedActor.from(jwt));
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
        Run revision = runs.revise(
                tenantId,
                runId,
                idempotencyKey,
                request.feedback().strip(),
                AuthenticatedActor.from(jwt));
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
        runs.events(tenantId, runId, lastEventId, AuthenticatedActor.from(jwt))
                .subscribe(new SseSubscriber(emitter, runId));
        return emitter;
    }

    @GetMapping("/projects/{projectId}/threads/{threadId}/messages")
    HistoryResponse history(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @PathVariable @Pattern(regexp = "[A-Za-z0-9._:-]{1,200}") String threadId,
            @AuthenticationPrincipal Jwt jwt) {
        List<MessageResponse> messages = runs.history(
                        tenantId,
                        projectId,
                        threadId,
                        AuthenticatedActor.from(jwt))
                .stream()
                .map(MessageResponse::from)
                .toList();
        return new HistoryResponse(messages);
    }

    @GetMapping("/projects/{projectId}/runtime-info")
    RuntimeInfoResponse info(
            @RequestHeader(ORGANIZATION_HEADER) UUID tenantId,
            @PathVariable UUID projectId,
            @AuthenticationPrincipal Jwt jwt) {
        return RuntimeInfoResponse.from(
                runs.info(tenantId, projectId, AuthenticatedActor.from(jwt)));
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
            @Size(max = 8) List<@Valid TeamMemberRequest> teamMembers) {

        StartRequest toServiceRequest() {
            List<TeamMember> members = teamMembers == null
                    ? List.of()
                    : teamMembers.stream().map(TeamMemberRequest::toService).toList();
            return new StartRequest(
                    message.strip(),
                    model,
                    threadId,
                    capabilityProfile.toService(),
                    members);
        }
    }

    record CapabilityProfileRequest(
            @NotBlank @Pattern(regexp = PROFILE_ID_PATTERN) String id,
            @NotBlank @Pattern(regexp = PROFILE_VERSION_PATTERN) String version) {

        ProfileSelector toService() {
            return new ProfileSelector(id, version);
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
