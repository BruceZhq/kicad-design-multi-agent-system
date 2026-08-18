package team.ratsnest.controlplane.agentgateway;

import java.time.Instant;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Flow;

public interface AgentRuntimeGateway {

    RuntimeRun startRun(StartRunCommand command);

    RuntimeRun getRun(RunReference reference);

    RuntimeRun controlRun(ControlRunCommand command);

    RuntimeRun resumeRun(ResumeRunCommand command);

    Flow.Publisher<RuntimeEvent> subscribeEvents(EventSubscription subscription);

    RuntimeInfo getInfo(RuntimeIdentity identity);

    List<RuntimeMessage> getHistory(HistoryQuery query);

    enum RunControl {
        CANCEL
    }

    enum RunState {
        QUEUED,
        RUNNING,
        WAITING_FOR_INPUT,
        COMPLETED,
        FAILED,
        CANCELLED,
        TIMED_OUT
    }

    record StartRunCommand(
            String requestId,
            String threadId,
            RuntimeIdentity identity,
            String message,
            String model,
            Double timeoutSeconds,
            Map<String, Object> config,
            boolean streamTokens) {

        public StartRunCommand {
            config = immutableMap(config);
        }
    }

    record RuntimeIdentity(
            String principalId,
            String tenantId,
            String projectId) {
    }

    record RunReference(String requestId, RuntimeIdentity identity, String runtimeChannel) {
    }

    record ControlRunCommand(RunReference run, RunControl control) {
    }

    record ResumeRunCommand(
            RunReference run,
            String interactionId,
            String responseRequestId,
            String answer,
            long stateVersion,
            String model,
            Double timeoutSeconds,
            Map<String, Object> config) {

        public ResumeRunCommand {
            config = immutableMap(config);
        }
    }

    record EventSubscription(StartRunCommand command, long lastEventId) {
    }

    record HistoryQuery(String threadId, RuntimeIdentity identity) {
    }

    record RuntimeInfo(
            List<Map<String, String>> agents,
            List<String> models,
            String defaultAgent,
            String defaultModel,
            List<CapabilityProfile> profiles) {

        public RuntimeInfo {
            agents = agents == null ? List.of() : agents.stream()
                    .map(AgentRuntimeGateway::immutableStringMap)
                    .toList();
            models = models == null ? List.of() : List.copyOf(models);
            profiles = profiles == null ? List.of() : List.copyOf(profiles);
        }
    }

    record CapabilityProfile(
            String id,
            String version,
            String digest,
            String title,
            String description) {
    }

    record RuntimeRun(
            String requestId,
            String runId,
            String kind,
            RunState state,
            String agentId,
            String threadId,
            Instant createdAt,
            Instant startedAt,
            Instant finishedAt,
            long eventCount,
            Long oldestEventId,
            Long newestEventId,
            String errorCode,
            String error,
            Map<String, Object> result) {

        public RuntimeRun {
            result = immutableMap(result);
        }

        public RuntimeRun(
                String requestId,
                String runId,
                String kind,
                RunState state,
                String agentId,
                String threadId,
                Instant createdAt,
                Instant startedAt,
                Instant finishedAt,
                long eventCount,
                Long oldestEventId,
                Long newestEventId,
                String errorCode,
                String error) {
            this(
                    requestId, runId, kind, state, agentId, threadId,
                    createdAt, startedAt, finishedAt, eventCount,
                    oldestEventId, newestEventId, errorCode, error, Map.of());
        }
    }

    record RuntimeEvent(
            Long eventId,
            String type,
            RuntimeMessage message,
            String content,
            String error,
            Map<String, Object> data) {

        public RuntimeEvent {
            data = immutableMap(data);
        }
    }

    record RuntimeMessage(
            String type,
            String content,
            List<Map<String, Object>> toolCalls,
            String toolCallId,
            String runId,
            Map<String, Object> responseMetadata,
            Map<String, Object> customData) {

        public RuntimeMessage {
            toolCalls = toolCalls == null
                    ? List.of()
                    : toolCalls.stream().map(AgentRuntimeGateway::immutableMap).toList();
            responseMetadata = immutableMap(responseMetadata);
            customData = immutableMap(customData);
        }
    }

    private static <K, V> Map<K, V> immutableMap(Map<K, V> value) {
        return value == null
                ? Map.of()
                : Collections.unmodifiableMap(new LinkedHashMap<>(value));
    }

    private static Map<String, String> immutableStringMap(Map<String, String> value) {
        return immutableMap(value);
    }
}
