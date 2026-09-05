package team.ratsnest.controlplane.agentgateway.infrastructure.grpc;

import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.TreeMap;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Primary;
import org.springframework.stereotype.Component;

import io.grpc.ClientInterceptor;
import io.grpc.ManagedChannel;
import io.grpc.ManagedChannelBuilder;
import io.grpc.Metadata;
import io.grpc.Status;
import io.grpc.StatusRuntimeException;
import io.grpc.stub.ClientCallStreamObserver;
import io.grpc.stub.ClientResponseObserver;
import io.grpc.stub.MetadataUtils;
import jakarta.annotation.PreDestroy;
import team.ratsnest.controlplane.agentgateway.domain.model.AgentRuntimeException;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway;
import team.ratsnest.controlplane.agentgateway.domain.port.RuntimeCredentials;
import team.ratsnest.controlplane.agentgateway.infrastructure.http.HttpAgentRuntimeGateway;
import team.ratsnest.runtime.v1.AgentRuntimeServiceGrpc;
import team.ratsnest.runtime.v1.ControlRunRequest;
import team.ratsnest.runtime.v1.GetRunRequest;
import team.ratsnest.runtime.v1.RunEvent;
import team.ratsnest.runtime.v1.ResumeRunRequest;
import team.ratsnest.runtime.v1.SubscribeRunEventsRequest;
import tools.jackson.databind.ObjectMapper;

/**
 * gRPC transport for the durable run boundary. Runtime metadata and history
 * deliberately remain on the signed compatibility HTTP API in protocol v1.
 */
@Component
@Primary
@ConditionalOnProperty(name = "ratsnest.agent-runtime.transport", havingValue = "grpc")
public final class GrpcAgentRuntimeGateway implements AgentRuntimeGateway {

    private static final String SERVICE = "/ratsnest.runtime.v1.AgentRuntimeService/";
    private static final String START_PATH = SERVICE + "StartRun";
    private static final String GET_PATH = SERVICE + "GetRun";
    private static final String CONTROL_PATH = SERVICE + "ControlRun";
    private static final String RESUME_PATH = SERVICE + "ResumeRun";
    private static final String EVENTS_PATH = SERVICE + "SubscribeRunEvents";
    private static final Metadata.Key<String> AUTHORIZATION = Metadata.Key.of(
            "authorization", Metadata.ASCII_STRING_MARSHALLER);
    private static final Metadata.Key<String> REQUEST_ID = Metadata.Key.of(
            "x-request-id", Metadata.ASCII_STRING_MARSHALLER);
    private static final long UNARY_TIMEOUT_SECONDS = 20;

    private final ManagedChannel stableChannel;
    private final ManagedChannel canaryChannel;
    private team.ratsnest.controlplane.agentgateway.application.RuntimeVersionRoutes versionRoutes;
    private final java.util.concurrent.ConcurrentMap<String, ManagedChannel> versionChannels =
            new java.util.concurrent.ConcurrentHashMap<>();

    @org.springframework.beans.factory.annotation.Autowired
    public void setVersionRoutes(team.ratsnest.controlplane.agentgateway.application.RuntimeVersionRoutes routes) {
        this.versionRoutes = routes;
    }

    @jakarta.annotation.PreDestroy
    public void closeVersionChannels() { versionChannels.values().forEach(ManagedChannel::shutdownNow); }
    private final RuntimeCredentials signer;
    private final ObjectMapper objectMapper;
    private final HttpAgentRuntimeGateway compatibilityGateway;

    public GrpcAgentRuntimeGateway(
            @Value("${ratsnest.agent-runtime.grpc-target:}") String target,
            @Value("${ratsnest.agent-runtime.grpc-plaintext:false}") boolean plaintext,
            @Value("${ratsnest.agent-runtime.canary-grpc-target:}") String canaryTarget,
            @Value("${ratsnest.agent-runtime.canary-grpc-plaintext:false}") boolean canaryPlaintext,
            RuntimeCredentials signer,
            ObjectMapper objectMapper,
            HttpAgentRuntimeGateway compatibilityGateway) {
        this(
                buildChannel(target, plaintext),
                optionalChannel(canaryTarget, canaryPlaintext),
                signer,
                objectMapper,
                compatibilityGateway);
    }

    GrpcAgentRuntimeGateway(
            ManagedChannel channel,
            RuntimeCredentials signer,
            ObjectMapper objectMapper,
            HttpAgentRuntimeGateway compatibilityGateway) {
        this(channel, null, signer, objectMapper, compatibilityGateway);
    }

    GrpcAgentRuntimeGateway(
            ManagedChannel stableChannel,
            ManagedChannel canaryChannel,
            RuntimeCredentials signer,
            ObjectMapper objectMapper,
            HttpAgentRuntimeGateway compatibilityGateway) {
        this.stableChannel = Objects.requireNonNull(stableChannel, "stableChannel");
        this.canaryChannel = canaryChannel;
        this.signer = Objects.requireNonNull(signer, "signer");
        this.objectMapper = Objects.requireNonNull(objectMapper, "objectMapper");
        this.compatibilityGateway = Objects.requireNonNull(
                compatibilityGateway, "compatibilityGateway");
    }

    @Override
    public RuntimeRun startRun(StartRunCommand command) {
        team.ratsnest.runtime.v1.StartRunRequest.Builder builder =
                team.ratsnest.runtime.v1.StartRunRequest.newBuilder()
                        .setRequestId(command.requestId())
                        .setThreadId(command.threadId())
                        .setIdentity(identity(command.identity()))
                        .setMessage(command.message())
                        .setConfigJson(canonicalJson(command.config()))
                        .setStreamTokens(command.streamTokens());
        if (command.model() != null) {
            builder.setModel(command.model());
        }
        if (command.timeoutSeconds() != null) {
            builder.setTimeoutSeconds(command.timeoutSeconds());
        }
        team.ratsnest.runtime.v1.StartRunRequest request = builder.build();
        try {
            return runtimeRun(blockingStub(
                            runtimeChannel(command.config()),
                            START_PATH,
                            request.toByteArray(),
                            command.identity(),
                            command.requestId())
                    .startRun(request));
        } catch (StatusRuntimeException exception) {
            throw runtimeFailure(exception);
        }
    }

    @Override
    public RuntimeRun getRun(RunReference reference) {
        GetRunRequest request = runRequest(reference);
        try {
            return runtimeRun(blockingStub(
                            reference.runtimeChannel(),
                            GET_PATH,
                            request.toByteArray(),
                            reference.identity(),
                            reference.requestId())
                    .getRun(request));
        } catch (StatusRuntimeException exception) {
            throw runtimeFailure(exception);
        }
    }

    @Override
    public RuntimeRun controlRun(ControlRunCommand command) {
        if (command.control() != RunControl.CANCEL) {
            throw new IllegalArgumentException("Unsupported Agent Runtime control");
        }
        ControlRunRequest request = ControlRunRequest.newBuilder()
                .setRun(runRequest(command.run()))
                .setControl(team.ratsnest.runtime.v1.RunControl.RUN_CONTROL_CANCEL)
                .build();
        try {
            return runtimeRun(blockingStub(
                            command.run().runtimeChannel(),
                            CONTROL_PATH,
                            request.toByteArray(),
                            command.run().identity(),
                            command.run().requestId())
                    .controlRun(request));
        } catch (StatusRuntimeException exception) {
            throw runtimeFailure(exception);
        }
    }

    @Override
    public RuntimeRun resumeRun(ResumeRunCommand command) {
        ResumeRunRequest.Builder builder = ResumeRunRequest.newBuilder()
                .setRun(runRequest(command.run()))
                .setInteractionId(command.interactionId())
                .setResponseRequestId(command.responseRequestId())
                .setAnswer(command.answer())
                .setStateVersion(command.stateVersion())
                .setConfigJson(canonicalJson(command.config()));
        if (command.model() != null) {
            builder.setModel(command.model());
        }
        if (command.timeoutSeconds() != null) {
            builder.setTimeoutSeconds(command.timeoutSeconds());
        }
        ResumeRunRequest request = builder.build();
        try {
            return runtimeRun(blockingStub(
                            command.run().runtimeChannel(),
                            RESUME_PATH,
                            request.toByteArray(),
                            command.run().identity(),
                            command.run().requestId())
                    .resumeRun(request));
        } catch (StatusRuntimeException exception) {
            throw runtimeFailure(exception);
        }
    }

    @Override
    public Flow.Publisher<RuntimeEvent> subscribeEvents(EventSubscription subscription) {
        return subscriber -> {
            GrpcEventSubscription stream = new GrpcEventSubscription(subscriber, subscription);
            subscriber.onSubscribe(stream);
            stream.start();
        };
    }

    @Override
    public RuntimeInfo getInfo(RuntimeIdentity identity) {
        return compatibilityGateway.getInfo(identity);
    }

    @Override
    public List<RuntimeMessage> getHistory(HistoryQuery query) {
        return compatibilityGateway.getHistory(query);
    }

    @PreDestroy
    void close() {
        stableChannel.shutdownNow();
        if (canaryChannel != null && canaryChannel != stableChannel) {
            canaryChannel.shutdownNow();
        }
    }

    private AgentRuntimeServiceGrpc.AgentRuntimeServiceBlockingStub blockingStub(
            String runtimeChannel,
            String path,
            byte[] body,
            RuntimeIdentity identity,
            String requestId) {
        return AgentRuntimeServiceGrpc.newBlockingStub(channel(runtimeChannel))
                .withInterceptors(headers(path, body, identity, requestId))
                .withDeadlineAfter(UNARY_TIMEOUT_SECONDS, TimeUnit.SECONDS);
    }

    private AgentRuntimeServiceGrpc.AgentRuntimeServiceStub asyncStub(
            String runtimeChannel,
            String path,
            byte[] body,
            RuntimeIdentity identity,
            String requestId) {
        return AgentRuntimeServiceGrpc.newStub(channel(runtimeChannel))
                .withInterceptors(headers(path, body, identity, requestId));
    }

    private ClientInterceptor headers(
            String path,
            byte[] body,
            RuntimeIdentity identity,
            String requestId) {
        Metadata metadata = new Metadata();
        metadata.put(
                AUTHORIZATION,
                "Bearer " + signer.token("POST", path, body, identity, requestId));
        metadata.put(REQUEST_ID, requestId);
        return MetadataUtils.newAttachHeadersInterceptor(metadata);
    }

    private GetRunRequest runRequest(RunReference reference) {
        return GetRunRequest.newBuilder()
                .setRequestId(reference.requestId())
                .setIdentity(identity(reference.identity()))
                .build();
    }

    private team.ratsnest.runtime.v1.RuntimeIdentity identity(RuntimeIdentity value) {
        return team.ratsnest.runtime.v1.RuntimeIdentity.newBuilder()
                .setPrincipalId(value.principalId())
                .setTenantId(value.tenantId())
                .setProjectId(value.projectId())
                .build();
    }

    private RuntimeRun runtimeRun(team.ratsnest.runtime.v1.Run value) {
        return new RuntimeRun(
                required(value.getRequestId(), "request_id"),
                value.hasGraphRunId() ? required(value.getGraphRunId(), "graph_run_id") : null,
                required(value.getKind(), "kind"),
                state(value.getState()),
                required(value.getAgentId(), "agent_id"),
                required(value.getThreadId(), "thread_id"),
                instant(value.getCreatedAt(), "created_at"),
                value.hasStartedAt() ? instant(value.getStartedAt(), "started_at") : null,
                value.hasFinishedAt() ? instant(value.getFinishedAt(), "finished_at") : null,
                unsigned(value.getEventCount(), "event_count"),
                value.hasOldestEventSeq()
                        ? unsigned(value.getOldestEventSeq(), "oldest_event_seq")
                        : null,
                value.hasNewestEventSeq()
                        ? unsigned(value.getNewestEventSeq(), "newest_event_seq")
                        : null,
                value.hasErrorCode() ? value.getErrorCode() : null,
                value.hasError() ? value.getError() : null,
                value.hasResultJson() ? runResult(value.getResultJson()) : Map.of(),
                value.getExecutionLeaseActive(),
                value.getRecoverable(),
                value.hasLeaseExpiresAt()
                        ? instant(value.getLeaseExpiresAt(), "lease_expires_at")
                        : null,
                instant(value.getCheckedAt(), "checked_at"));
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> runResult(String value) {
        try {
            Object parsed = objectMapper.readValue(value, Object.class);
            if (!(parsed instanceof Map<?, ?> result)) {
                throw new AgentRuntimeException(502, "Agent Runtime returned an invalid run result");
            }
            return (Map<String, Object>) result;
        } catch (AgentRuntimeException exception) {
            throw exception;
        } catch (Exception exception) {
            throw new AgentRuntimeException(502, "Agent Runtime returned invalid run result JSON");
        }
    }

    private RunState state(team.ratsnest.runtime.v1.RunState value) {
        return switch (value) {
            case RUN_STATE_QUEUED -> RunState.QUEUED;
            case RUN_STATE_RUNNING -> RunState.RUNNING;
            case RUN_STATE_WAITING_FOR_INPUT -> RunState.WAITING_FOR_INPUT;
            case RUN_STATE_COMPLETED -> RunState.COMPLETED;
            case RUN_STATE_FAILED -> RunState.FAILED;
            case RUN_STATE_CANCELLED -> RunState.CANCELLED;
            case RUN_STATE_TIMED_OUT -> RunState.TIMED_OUT;
            default -> throw new AgentRuntimeException(502, "Agent Runtime returned an invalid run state");
        };
    }

    private RuntimeEvent runtimeEvent(RunEvent event, EventSubscription subscription) {
        long sequence = unsigned(event.getEventSeq(), "event_seq");
        if (sequence == 0) {
            throw new AgentRuntimeException(502, "Agent Runtime returned a zero event sequence");
        }
        String expectedRunId = subscription.command().requestId();
        if (!event.getRunId().isBlank() && !event.getRunId().equals(expectedRunId)) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an event for another run");
        }

        String type = required(event.getType(), "event type");
        Map<String, Object> envelope = event.getPayloadJson().isBlank()
                ? Map.of()
                : jsonObject(event.getPayloadJson());
        Object envelopeType = envelope.get("type");
        if (envelopeType != null && !type.equals(envelopeType.toString())) {
            throw new AgentRuntimeException(502, "Agent Runtime event type does not match its payload");
        }
        if ("message".equals(type)) {
            return new RuntimeEvent(
                    sequence,
                    type,
                    runtimeMessage(object(envelope.get("content"))),
                    null,
                    null,
                    Map.of());
        }
        if ("artifact_manifest".equals(type)) {
            return new RuntimeEvent(
                    sequence,
                    type,
                    null,
                    null,
                    null,
                    object(envelope.get("content")));
        }
        if ("ag_ui".equals(type)) {
            return new RuntimeEvent(
                    sequence,
                    type,
                    null,
                    null,
                    null,
                    object(envelope.get("content")));
        }
        if ("token".equals(type) || "reasoning".equals(type)) {
            return new RuntimeEvent(
                    sequence,
                    type,
                    null,
                    nullableText(envelope.get("content")),
                    null,
                    Map.of());
        }
        if ("error".equals(type)) {
            Map<String, Object> data = new LinkedHashMap<>();
            copyPresent(envelope, data, "code");
            copyPresent(envelope, data, "retryable");
            return new RuntimeEvent(
                    sequence,
                    type,
                    null,
                    null,
                    nullableText(envelope.get("content")),
                    data);
        }
        if (terminalEvent(type)) {
            String error = nullableText(envelope.get("error"));
            if (error == null && !"completed".equals(type)) {
                error = nullableText(envelope.get("content"));
            }
            return new RuntimeEvent(sequence, type, null, null, error, eventData(envelope));
        }
        return new RuntimeEvent(sequence, type, null, null, null, eventData(envelope));
    }

    private boolean terminalEvent(String type) {
        return "completed".equals(type)
                || "failed".equals(type)
                || "cancelled".equals(type)
                || "timed_out".equals(type);
    }

    private Map<String, Object> eventData(Map<String, Object> envelope) {
        if (envelope.isEmpty()) {
            return Map.of();
        }
        Map<String, Object> data = new LinkedHashMap<>(envelope);
        data.remove("type");
        data.remove("content");
        data.remove("error");
        return Collections.unmodifiableMap(data);
    }

    private RuntimeMessage runtimeMessage(Map<String, Object> value) {
        return new RuntimeMessage(
                required(nullableText(value.get("type")), "message type"),
                Objects.requireNonNullElse(nullableText(value.get("content")), ""),
                objectList(value.get("tool_calls")),
                nullableText(value.get("tool_call_id")),
                nullableText(value.get("run_id")),
                objectOrEmpty(value.get("response_metadata")),
                objectOrEmpty(value.get("custom_data")));
    }

    private String canonicalJson(Map<String, Object> value) {
        try {
            return objectMapper.writeValueAsString(canonicalValue(value));
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to serialize Agent Runtime configuration", exception);
        }
    }

    private Object canonicalValue(Object value) {
        if (value instanceof Map<?, ?> map) {
            Map<String, Object> sorted = new TreeMap<>();
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!(entry.getKey() instanceof String key)) {
                    throw new IllegalArgumentException("Agent Runtime configuration keys must be strings");
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

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonObject(String value) {
        try {
            Object parsed = objectMapper.readValue(value, Object.class);
            return object(parsed);
        } catch (AgentRuntimeException exception) {
            throw exception;
        } catch (Exception exception) {
            throw new AgentRuntimeException(502, "Agent Runtime returned invalid event JSON");
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> object(Object value) {
        if (!(value instanceof Map<?, ?> map)) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an invalid event object");
        }
        return (Map<String, Object>) map;
    }

    private Map<String, Object> objectOrEmpty(Object value) {
        return value instanceof Map<?, ?> ? object(value) : Map.of();
    }

    private List<Map<String, Object>> objectList(Object value) {
        if (!(value instanceof List<?> list)) {
            return List.of();
        }
        return list.stream().map(this::object).toList();
    }

    private String nullableText(Object value) {
        return value == null ? null : Objects.toString(value);
    }

    private String required(String value, String field) {
        if (value == null || value.isBlank()) {
            throw new AgentRuntimeException(502, "Agent Runtime response is missing " + field);
        }
        return value;
    }

    private Instant instant(String value, String field) {
        try {
            return Instant.parse(required(value, field));
        } catch (DateTimeParseException exception) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an invalid " + field);
        }
    }

    private long unsigned(long value, String field) {
        if (value < 0) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an unsupported " + field);
        }
        return value;
    }

    private void copyPresent(Map<String, Object> source, Map<String, Object> target, String key) {
        Object value = source.get(key);
        if (value != null) {
            target.put(key, value);
        }
    }

    private AgentRuntimeException runtimeFailure(StatusRuntimeException exception) {
        Status status = exception.getStatus();
        int httpStatus = switch (status.getCode()) {
            case INVALID_ARGUMENT, OUT_OF_RANGE -> 400;
            case UNAUTHENTICATED -> 401;
            case PERMISSION_DENIED -> 403;
            case NOT_FOUND -> 404;
            case ALREADY_EXISTS, ABORTED, FAILED_PRECONDITION -> 409;
            case DEADLINE_EXCEEDED -> 504;
            case UNAVAILABLE, RESOURCE_EXHAUSTED -> 503;
            default -> 502;
        };
        String detail = status.getDescription();
        return new AgentRuntimeException(
                httpStatus,
                detail == null || detail.isBlank()
                        ? "Agent Runtime gRPC request failed: " + status.getCode()
                        : detail);
    }

    private static ManagedChannel buildChannel(String target, boolean plaintext) {
        if (target == null || target.isBlank()) {
            throw new IllegalStateException("Agent Runtime gRPC target is required");
        }
        ManagedChannelBuilder<?> builder = ManagedChannelBuilder.forTarget(target.strip());
        if (plaintext) {
            builder.usePlaintext();
        }
        return builder.build();
    }

    private static ManagedChannel optionalChannel(String target, boolean plaintext) {
        return target == null || target.isBlank() ? null : buildChannel(target, plaintext);
    }

    private ManagedChannel channel(String runtimeChannel) {
        var endpoint = versionRoutes == null ? null : versionRoutes.endpoint(runtimeChannel);
        if (endpoint != null) {
            if (!(endpoint.get("grpc") instanceof String target) || target.isBlank()) {
                throw new AgentRuntimeException(503, "Version-pinned gRPC deployment is not configured");
            }
            return versionChannels.computeIfAbsent(target, key -> buildChannel(key,
                    Boolean.TRUE.equals(endpoint.get("plaintext"))));
        }
        if (!team.ratsnest.controlplane.agentgateway.application.RuntimeVersionRoutes.canary(runtimeChannel)) {
            return stableChannel;
        }
        if (canaryChannel == null) {
            throw new AgentRuntimeException(
                    503, "Canary Agent Runtime gRPC endpoint is not configured");
        }
        return canaryChannel;
    }

    private String runtimeChannel(Map<String, Object> config) {
        return team.ratsnest.controlplane.agentgateway.application.RuntimeVersionRoutes.selector(config);
    }

    private final class GrpcEventSubscription
            implements Flow.Subscription,
                    ClientResponseObserver<SubscribeRunEventsRequest, RunEvent> {

        private final Flow.Subscriber<? super RuntimeEvent> subscriber;
        private final EventSubscription subscription;
        private final SubscribeRunEventsRequest request;
        private ClientCallStreamObserver<SubscribeRunEventsRequest> stream;
        private boolean cancelled;
        private boolean started;
        private boolean cancelPending;
        private long pendingDemand;
        private long lastSequence;
        private boolean pauseSeen;
        private boolean terminalSeen;

        private GrpcEventSubscription(
                Flow.Subscriber<? super RuntimeEvent> subscriber,
                EventSubscription subscription) {
            this.subscriber = Objects.requireNonNull(subscriber, "subscriber");
            this.subscription = Objects.requireNonNull(subscription, "subscription");
            this.lastSequence = subscription.lastEventId();
            GetRunRequest run = GetRunRequest.newBuilder()
                    .setRequestId(subscription.command().requestId())
                    .setIdentity(identity(subscription.command().identity()))
                    .build();
            this.request = SubscribeRunEventsRequest.newBuilder()
                    .setRun(run)
                    .setLastEventSeq(subscription.lastEventId())
                    .build();
        }

        private void start() {
            try {
                asyncStub(
                                runtimeChannel(subscription.command().config()),
                                EVENTS_PATH,
                                request.toByteArray(),
                                subscription.command().identity(),
                                subscription.command().requestId())
                        .subscribeRunEvents(request, this);
                synchronized (this) {
                    started = true;
                    if (cancelPending && stream != null) {
                        cancelPending = false;
                        stream.cancel("downstream subscription cancelled", null);
                    } else if (!cancelled && pendingDemand > 0) {
                        long demand = pendingDemand;
                        pendingDemand = 0;
                        requestUpstream(demand);
                    }
                }
            } catch (RuntimeException exception) {
                onError(exception);
            }
        }

        @Override
        public synchronized void beforeStart(
                ClientCallStreamObserver<SubscribeRunEventsRequest> requestStream) {
            this.stream = requestStream;
            int initialRequest = cancelled
                    ? 0
                    : (int) Math.min(pendingDemand, Integer.MAX_VALUE);
            pendingDemand -= initialRequest;
            requestStream.disableAutoRequestWithInitial(initialRequest);
        }

        @Override
        public synchronized void request(long count) {
            if (count <= 0) {
                cancel();
                subscriber.onError(new IllegalArgumentException("Flow demand must be positive"));
                return;
            }
            if (cancelled) {
                return;
            }
            if (stream == null || !started) {
                pendingDemand = count == Long.MAX_VALUE || pendingDemand > Long.MAX_VALUE - count
                        ? Long.MAX_VALUE
                        : pendingDemand + count;
                return;
            }
            requestUpstream(count);
        }

        @Override
        public synchronized void cancel() {
            if (cancelled) {
                return;
            }
            cancelled = true;
            if (stream != null && started) {
                stream.cancel("downstream subscription cancelled", null);
            } else {
                cancelPending = true;
            }
        }

        @Override
        public void onNext(RunEvent event) {
            try {
                RuntimeEvent mapped;
                synchronized (this) {
                    if (cancelled) {
                        return;
                    }
                    mapped = runtimeEvent(event, subscription);
                    if (mapped.eventId() <= lastSequence) {
                        throw new AgentRuntimeException(
                                502, "Agent Runtime event sequence is not strictly increasing");
                    }
                    lastSequence = mapped.eventId();
                    if ("ag_ui".equals(mapped.type())
                            && "ratsnest.human-input-required.v1".equals(mapped.data().get("name"))) {
                        pauseSeen = true;
                    }
                    if (terminalEvent(mapped.type())) {
                        terminalSeen = true;
                    }
                }
                subscriber.onNext(mapped);
            } catch (RuntimeException exception) {
                synchronized (this) {
                    if (cancelled) {
                        return;
                    }
                    cancel();
                }
                subscriber.onError(exception);
            }
        }

        @Override
        public synchronized void onError(Throwable throwable) {
            if (cancelled) {
                return;
            }
            cancelled = true;
            subscriber.onError(throwable instanceof StatusRuntimeException status
                    ? runtimeFailure(status)
                    : throwable);
        }

        @Override
        public synchronized void onCompleted() {
            if (cancelled) {
                return;
            }
            cancelled = true;
            if (pauseSeen || terminalSeen) {
                subscriber.onComplete();
            } else {
                subscriber.onError(new AgentRuntimeException(
                        502, "Agent Runtime ended its event stream before publishing a terminal or waiting state"));
            }
        }

        private void requestUpstream(long count) {
            if (count > 0) {
                stream.request((int) Math.min(count, Integer.MAX_VALUE));
            }
        }
    }
}
