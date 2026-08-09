package team.ratsnest.controlplane.agentgateway;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Flow;
import java.util.regex.Pattern;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import jakarta.annotation.PreDestroy;
import team.ratsnest.controlplane.agentgateway.AgentRuntimeGateway.CapabilityProfile;
import tools.jackson.databind.ObjectMapper;

@Component
public final class HttpAgentRuntimeGateway implements AgentRuntimeGateway {

    private static final String AGENT_ID = "ratsnestpro-multi-agent";
    private static final String HISTORY_REQUEST_ID = "history-query";
    private static final byte[] EMPTY_BODY = new byte[0];
    private static final Duration UNARY_TIMEOUT = Duration.ofSeconds(20);
    private static final Pattern PROFILE_ID = Pattern.compile("[a-z0-9][a-z0-9-]{1,63}");
    private static final Pattern PROFILE_VERSION = Pattern.compile(
            "(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)(?:\\.(?:0|[1-9][0-9]*))?");
    private static final Pattern PROFILE_DIGEST = Pattern.compile("[0-9a-f]{64}");
    private static final Set<String> PROFILE_FIELDS = Set.of(
            "id", "version", "digest", "title", "description");

    private final URI baseUri;
    private final InternalTaskSigner signer;
    private final ObjectMapper objectMapper;
    private final ExecutorService executor;
    private final HttpClient httpClient;

    public HttpAgentRuntimeGateway(
            @Value("${ratsnest.agent-runtime.base-url:}") String baseUrl,
            InternalTaskSigner signer,
            ObjectMapper objectMapper) {
        this.baseUri = validateBaseUri(baseUrl);
        this.signer = signer;
        this.objectMapper = objectMapper;
        this.executor = Executors.newVirtualThreadPerTaskExecutor();
        this.httpClient = HttpClient.newBuilder()
                // Uvicorn exposes this compatibility transport as HTTP/1.1.
                // Java's default client may attempt a clear-text h2c upgrade;
                // requests with bodies are then rejected by Uvicorn's h11 parser.
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofSeconds(10))
                .executor(executor)
                .build();
    }

    @Override
    public RuntimeRun startRun(StartRunCommand command) {
        byte[] body = jsonBytes(streamBody(command, 0));
        HttpResponse<InputStream> response = send(
                request("POST", streamPath(), body, command.identity(), command.requestId(), "text/event-stream"),
                HttpResponse.BodyHandlers.ofInputStream());
        try {
            requireStreamSuccess(response);
        } finally {
            try {
                response.body().close();
            } catch (IOException ignored) {
            }
        }
        Instant now = Instant.now();
        return new RuntimeRun(
                command.requestId(),
                null,
                "stream",
                RunState.RUNNING,
                AGENT_ID,
                command.threadId(),
                now,
                now,
                null,
                0,
                null,
                null,
                null,
                null);
    }

    @Override
    public RuntimeRun getRun(RunReference reference) {
        String path = "/internal/v1/runs/" + encodePathSegment(reference.requestId());
        HttpResponse<byte[]> response = send(
                request("GET", path, EMPTY_BODY, reference.identity(), reference.requestId(), "application/json"),
                HttpResponse.BodyHandlers.ofByteArray());
        requireSuccess(response.statusCode(), response.body());
        Map<String, Object> value = jsonObject(response.body());
        return new RuntimeRun(
                text(value, "request_id"),
                nullableText(value.get("run_id")),
                text(value, "kind"),
                state(text(value, "status")),
                text(value, "agent_id"),
                text(value, "thread_id"),
                instant(value.get("created_at")),
                nullableInstant(value.get("started_at")),
                nullableInstant(value.get("finished_at")),
                number(value.get("event_count"), "event_count"),
                nullableNumber(value.get("oldest_event_id"), "oldest_event_id"),
                nullableNumber(value.get("newest_event_id"), "newest_event_id"),
                nullableText(value.get("error_code")),
                nullableText(value.get("error")),
                runtimeResult(value));
    }

    @Override
    public RuntimeRun controlRun(ControlRunCommand command) {
        if (command.control() != RunControl.CANCEL) {
            throw new IllegalArgumentException("Unsupported Agent Runtime control");
        }
        RunReference reference = command.run();
        String path = "/internal/v1/runs/" + encodePathSegment(reference.requestId());
        HttpResponse<byte[]> response = send(
                request("DELETE", path, EMPTY_BODY, reference.identity(), reference.requestId(), "application/json"),
                HttpResponse.BodyHandlers.ofByteArray());
        requireSuccess(response.statusCode(), response.body());
        return getRun(reference);
    }

    @Override
    public Flow.Publisher<RuntimeEvent> subscribeEvents(EventSubscription subscription) {
        return subscriber -> {
            EventStreamSubscription stream = new EventStreamSubscription(subscriber, subscription);
            subscriber.onSubscribe(stream);
            executor.submit(stream::run);
        };
    }

    @Override
    public RuntimeInfo getInfo(RuntimeIdentity identity) {
        String path = "/internal/v1/info";
        HttpResponse<byte[]> response = send(
                request("GET", path, EMPTY_BODY, identity, "metadata", "application/json"),
                HttpResponse.BodyHandlers.ofByteArray());
        requireSuccess(response.statusCode(), response.body());
        Map<String, Object> value = jsonObject(response.body());
        List<Map<String, String>> agents = requiredObjectList(value.get("agents"), "agents").stream()
                .map(agent -> Map.of(
                        "key", text(agent, "key"),
                        "description", text(agent, "description")))
                .toList();
        List<CapabilityProfile> profiles = capabilityProfiles(value.get("profiles"));
        return new RuntimeInfo(
                agents,
                stringList(value.get("models"), "models"),
                text(value, "default_agent"),
                text(value, "default_model"),
                profiles);
    }

    @Override
    public List<RuntimeMessage> getHistory(HistoryQuery query) {
        String path = "/internal/v1/history";
        byte[] body = jsonBytes(Map.of(
                "request_id", HISTORY_REQUEST_ID,
                "thread_id", query.threadId()));
        HttpResponse<byte[]> response = send(
                request("POST", path, body, query.identity(), HISTORY_REQUEST_ID, "application/json"),
                HttpResponse.BodyHandlers.ofByteArray());
        requireSuccess(response.statusCode(), response.body());
        return requiredObjectList(
                        jsonObject(response.body()).get("messages"),
                        "messages")
                .stream()
                .map(this::runtimeMessage)
                .toList();
    }

    @PreDestroy
    void close() {
        executor.shutdownNow();
    }

    private Map<String, Object> streamBody(StartRunCommand command, long lastEventId) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("message", command.message());
        body.put("model", command.model());
        body.put("thread_id", command.threadId());
        body.put("request_id", command.requestId());
        body.put("timeout_seconds", command.timeoutSeconds());
        body.put("agent_config", command.config());
        body.put("stream_tokens", command.streamTokens());
        body.put("last_event_id", lastEventId);
        return body;
    }

    private HttpRequest request(
            String method,
            String path,
            byte[] body,
            RuntimeIdentity identity,
            String runId,
            String accept) {
        return requestBuilder(method, path, body, identity, runId, accept)
                .timeout(UNARY_TIMEOUT)
                .build();
    }

    private HttpRequest streamingRequest(
            String method,
            String path,
            byte[] body,
            RuntimeIdentity identity,
            String runId,
            String accept) {
        return requestBuilder(method, path, body, identity, runId, accept).build();
    }

    private HttpRequest.Builder requestBuilder(
            String method,
            String path,
            byte[] body,
            RuntimeIdentity identity,
            String runId,
            String accept) {
        HttpRequest.BodyPublisher publisher = body.length == 0
                ? HttpRequest.BodyPublishers.noBody()
                : HttpRequest.BodyPublishers.ofByteArray(body);
        return HttpRequest.newBuilder(baseUri.resolve(path))
                .header("Accept", accept)
                .header("Content-Type", "application/json")
                .header("Authorization", "Bearer " + signer.token(method, path, body, identity, runId))
                .header("X-Request-ID", runId)
                .method(method, publisher);
    }

    private String streamPath() {
        return "/internal/v1/runs/" + AGENT_ID + "/stream";
    }

    private <T> HttpResponse<T> send(
            HttpRequest request,
            HttpResponse.BodyHandler<T> bodyHandler) {
        try {
            return httpClient.send(request, bodyHandler);
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw unavailable(exception);
        } catch (IOException exception) {
            throw unavailable(exception);
        }
    }

    private void requireSuccess(int status, byte[] body) {
        if (status >= 200 && status < 300) {
            return;
        }
        String detail = body == null ? "" : new String(body, StandardCharsets.UTF_8);
        if (detail.length() > 2_000) {
            detail = detail.substring(0, 2_000);
        }
        throw new AgentRuntimeException(status, detail.isBlank()
                ? "Agent Runtime request failed with HTTP " + status
                : detail);
    }

    private void requireStreamSuccess(HttpResponse<InputStream> response) {
        if (response.statusCode() >= 200 && response.statusCode() < 300) {
            return;
        }
        try {
            requireSuccess(response.statusCode(), response.body().readNBytes(2_001));
        } catch (IOException exception) {
            throw unavailable(exception);
        }
    }

    private AgentRuntimeException unavailable(Exception exception) {
        return new AgentRuntimeException(503, "Agent Runtime is unavailable: " + exception.getMessage());
    }

    private RuntimeEvent runtimeEvent(Long eventId, String raw, StartRunCommand command) {
        if ("[DONE]".equals(raw)) {
            RuntimeRun run = awaitTerminalRun(command);
            String type = switch (run.state()) {
                case COMPLETED -> "completed";
                case CANCELLED -> "cancelled";
                case TIMED_OUT -> "timed_out";
                case FAILED -> "failed";
                default -> throw new AgentRuntimeException(
                        502,
                        "Agent Runtime ended its event stream before publishing a terminal state");
            };
            return new RuntimeEvent(eventId, type, null, null, run.error(), Map.of());
        }

        Map<String, Object> envelope = jsonObject(raw.getBytes(StandardCharsets.UTF_8));
        if (eventId == null && envelope.get("event_id") instanceof Number number) {
            eventId = number.longValue();
        }
        String type = text(envelope, "type");
        if ("message".equals(type)) {
            RuntimeMessage message = runtimeMessage(object(envelope.get("content")));
            return new RuntimeEvent(eventId, "message", message, null, null, Map.of());
        }
        if ("artifact_manifest".equals(type)) {
            return new RuntimeEvent(
                    eventId,
                    type,
                    null,
                    null,
                    null,
                    object(envelope.get("content")));
        }
        if ("token".equals(type) || "reasoning".equals(type)) {
            return new RuntimeEvent(
                    eventId,
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
            String error = nullableText(envelope.get("content"));
            return new RuntimeEvent(eventId, "error", null, null, error, data);
        }
        throw new AgentRuntimeException(502, "Agent Runtime emitted an unsupported event type");
    }

    private RuntimeRun awaitTerminalRun(StartRunCommand command) {
        RunReference reference = new RunReference(command.requestId(), command.identity());
        RuntimeRun run = getRun(reference);
        for (int attempt = 0; !isTerminal(run.state()) && attempt < 20; attempt++) {
            try {
                Thread.sleep(25);
            } catch (InterruptedException exception) {
                Thread.currentThread().interrupt();
                throw unavailable(exception);
            }
            run = getRun(reference);
        }
        return run;
    }

    private boolean isTerminal(RunState state) {
        return state == RunState.COMPLETED
                || state == RunState.FAILED
                || state == RunState.CANCELLED
                || state == RunState.TIMED_OUT;
    }

    private RuntimeMessage runtimeMessage(Map<String, Object> value) {
        List<Map<String, Object>> toolCalls = objectList(value.get("tool_calls"));
        return new RuntimeMessage(
                text(value, "type"),
                nullableText(value.get("content")) == null ? "" : nullableText(value.get("content")),
                toolCalls,
                nullableText(value.get("tool_call_id")),
                nullableText(value.get("run_id")),
                objectOrEmpty(value.get("response_metadata")),
                objectOrEmpty(value.get("custom_data")));
    }

    private byte[] jsonBytes(Object value) {
        try {
            return objectMapper.writeValueAsBytes(value);
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to serialize Agent Runtime request", exception);
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> jsonObject(byte[] value) {
        try {
            Object parsed = objectMapper.readValue(value, Object.class);
            return object(parsed);
        } catch (Exception exception) {
            throw new AgentRuntimeException(502, "Agent Runtime returned invalid JSON");
        }
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> object(Object value) {
        if (!(value instanceof Map<?, ?> map)) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an invalid object");
        }
        return (Map<String, Object>) map;
    }

    private Map<String, Object> objectOrEmpty(Object value) {
        return value instanceof Map<?, ?> ? object(value) : Map.of();
    }

    private Map<String, Object> runtimeResult(Map<String, Object> value) {
        Map<String, Object> result = objectOrEmpty(value.get("result"));
        if (!result.isEmpty() || !(value.get("artifact_manifest") instanceof Map<?, ?>)) {
            return result;
        }
        return Map.of("artifact_manifest", object(value.get("artifact_manifest")));
    }

    private List<Map<String, Object>> objectList(Object value) {
        if (!(value instanceof List<?> list)) {
            return List.of();
        }
        return list.stream().map(this::object).toList();
    }

    private List<Map<String, Object>> requiredObjectList(Object value, String name) {
        if (!(value instanceof List<?> list)) {
            throw new AgentRuntimeException(502, "Agent Runtime response is missing " + name);
        }
        return list.stream().map(this::object).toList();
    }

    private List<String> stringList(Object value, String name) {
        if (!(value instanceof List<?> list)) {
            throw new AgentRuntimeException(502, "Agent Runtime response is missing " + name);
        }
        return list.stream().map(item -> {
            if (!(item instanceof String text)) {
                throw new AgentRuntimeException(502, "Agent Runtime returned an invalid " + name);
            }
            return text;
        }).toList();
    }

    private List<CapabilityProfile> capabilityProfiles(Object value) {
        List<Map<String, Object>> items = requiredObjectList(value, "profiles");
        if (items.isEmpty()) {
            throw new AgentRuntimeException(502, "Agent Runtime returned no capability profiles");
        }
        Set<String> references = new HashSet<>();
        List<CapabilityProfile> profiles = new ArrayList<>();
        for (Map<String, Object> item : items) {
            if (!item.keySet().equals(PROFILE_FIELDS)) {
                throw new AgentRuntimeException(502, "Agent Runtime returned invalid capability profile fields");
            }
            CapabilityProfile profile = new CapabilityProfile(
                    profileText(item, "id", PROFILE_ID, 64),
                    profileText(item, "version", PROFILE_VERSION, 32),
                    profileText(item, "digest", PROFILE_DIGEST, 64),
                    profileText(item, "title", null, 120),
                    profileText(item, "description", null, 500));
            if (!references.add(profile.id() + "@" + profile.version())) {
                throw new AgentRuntimeException(502, "Agent Runtime returned duplicate capability profiles");
            }
            profiles.add(profile);
        }
        return List.copyOf(profiles);
    }

    private String profileText(
            Map<String, Object> value,
            String key,
            Pattern pattern,
            int maxLength) {
        Object raw = value.get(key);
        if (!(raw instanceof String text)
                || text.isBlank()
                || !text.equals(text.strip())
                || text.length() > maxLength
                || (pattern != null && !pattern.matcher(text).matches())) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an invalid profile " + key);
        }
        return text;
    }

    private String text(Map<String, Object> value, String key) {
        String result = nullableText(value.get(key));
        if (result == null || result.isBlank()) {
            throw new AgentRuntimeException(502, "Agent Runtime response is missing " + key);
        }
        return result;
    }

    private String nullableText(Object value) {
        return value == null ? null : Objects.toString(value);
    }

    private long number(Object value, String name) {
        if (!(value instanceof Number number)) {
            throw new AgentRuntimeException(502, "Agent Runtime response is missing " + name);
        }
        long result = number.longValue();
        if (result < 0) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an invalid " + name);
        }
        return result;
    }

    private Long nullableNumber(Object value, String name) {
        if (value == null) {
            return null;
        }
        if (!(value instanceof Number number)) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an invalid " + name);
        }
        long result = number.longValue();
        if (result < 0) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an invalid " + name);
        }
        return result;
    }

    private Instant instant(Object value) {
        String text = nullableText(value);
        if (text == null) {
            throw new AgentRuntimeException(502, "Agent Runtime response is missing a timestamp");
        }
        return parseInstant(text);
    }

    private Instant nullableInstant(Object value) {
        String text = nullableText(value);
        return text == null ? null : parseInstant(text);
    }

    private Instant parseInstant(String value) {
        try {
            return Instant.parse(value);
        } catch (java.time.format.DateTimeParseException exception) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an invalid timestamp");
        }
    }

    private RunState state(String value) {
        try {
            return RunState.valueOf(value.toUpperCase(java.util.Locale.ROOT));
        } catch (IllegalArgumentException exception) {
            throw new AgentRuntimeException(502, "Agent Runtime returned an invalid run state");
        }
    }

    private void copyPresent(Map<String, Object> source, Map<String, Object> target, String key) {
        Object value = source.get(key);
        if (value != null) {
            target.put(key, value);
        }
    }

    private String encodePathSegment(String value) {
        if (value == null || !value.matches("[A-Za-z0-9._:-]{1,200}")) {
            throw new IllegalArgumentException("Invalid Agent Runtime identifier");
        }
        return value;
    }

    private URI validateBaseUri(String value) {
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("Agent Runtime base URL is required");
        }
        URI uri = URI.create(value.endsWith("/") ? value : value + "/");
        if (!("http".equalsIgnoreCase(uri.getScheme()) || "https".equalsIgnoreCase(uri.getScheme()))
                || uri.getHost() == null
                || uri.getUserInfo() != null
                || uri.getQuery() != null
                || uri.getFragment() != null) {
            throw new IllegalStateException("Agent Runtime base URL must be an HTTP endpoint");
        }
        return uri;
    }

    private final class EventStreamSubscription implements Flow.Subscription {

        private final Flow.Subscriber<? super RuntimeEvent> subscriber;
        private final EventSubscription subscription;
        private final Object demandMonitor = new Object();
        private volatile boolean cancelled;
        private volatile InputStream input;
        private long demand;

        EventStreamSubscription(
                Flow.Subscriber<? super RuntimeEvent> subscriber,
                EventSubscription subscription) {
            this.subscriber = subscriber;
            this.subscription = subscription;
        }

        @Override
        public void request(long count) {
            if (count <= 0) {
                cancel();
                subscriber.onError(new IllegalArgumentException("Flow demand must be positive"));
                return;
            }
            synchronized (demandMonitor) {
                demand = count == Long.MAX_VALUE || demand > Long.MAX_VALUE - count
                        ? Long.MAX_VALUE
                        : demand + count;
                demandMonitor.notifyAll();
            }
        }

        @Override
        public void cancel() {
            cancelled = true;
            synchronized (demandMonitor) {
                demandMonitor.notifyAll();
            }
            InputStream current = input;
            if (current != null) {
                try {
                    current.close();
                } catch (IOException ignored) {
                }
            }
        }

        void run() {
            StartRunCommand command = subscription.command();
            byte[] body = jsonBytes(streamBody(command, subscription.lastEventId()));
            try {
                HttpResponse<InputStream> response = send(
                        HttpAgentRuntimeGateway.this.streamingRequest(
                                "POST",
                                streamPath(),
                                body,
                                command.identity(),
                                command.requestId(),
                                "text/event-stream"),
                        HttpResponse.BodyHandlers.ofInputStream());
                input = response.body();
                requireStreamSuccess(response);
                readEvents(input, command);
                if (!cancelled) {
                    subscriber.onComplete();
                }
            } catch (Exception exception) {
                if (!cancelled) {
                    subscriber.onError(exception);
                }
            } finally {
                cancel();
            }
        }

        private void readEvents(InputStream stream, StartRunCommand command) throws IOException {
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(stream, StandardCharsets.UTF_8))) {
                Long eventId = null;
                List<String> data = new ArrayList<>();
                String line;
                while (!cancelled && (line = reader.readLine()) != null) {
                    if (line.isEmpty()) {
                        if (!data.isEmpty()) {
                            emit(runtimeEvent(eventId, String.join("\n", data), command));
                        }
                        eventId = null;
                        data.clear();
                    } else if (line.startsWith("id:")) {
                        String rawId = line.substring(3).strip();
                        if (rawId.matches("\\d+")) {
                            eventId = Long.parseLong(rawId);
                        }
                    } else if (line.startsWith("data:")) {
                        data.add(line.substring(5).stripLeading());
                    } else if (line.startsWith(":")) {
                        emit(new RuntimeEvent(null, "heartbeat", null, null, null, Map.of()));
                    }
                }
                if (!cancelled && !data.isEmpty()) {
                    emit(runtimeEvent(eventId, String.join("\n", data), command));
                }
            }
        }

        private void emit(RuntimeEvent event) {
            synchronized (demandMonitor) {
                while (!cancelled && demand == 0) {
                    try {
                        demandMonitor.wait();
                    } catch (InterruptedException exception) {
                        Thread.currentThread().interrupt();
                        cancel();
                    }
                }
                if (cancelled) {
                    return;
                }
                if (demand != Long.MAX_VALUE) {
                    demand--;
                }
            }
            subscriber.onNext(event);
        }
    }
}
