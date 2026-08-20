package team.ratsnest.controlplane.run.application.model;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.run.domain.model.Run;

/** Stable application projection used by the runtime-status HTTP endpoint. */
public record RunRuntimeStatus(
        UUID runId,
        RunState controlState,
        RunState runtimeState,
        RunExecutionStatus executionStatus,
        Boolean leaseActive,
        Boolean recoverable,
        Instant leaseExpiresAt,
        long lastEventId,
        long eventCount,
        Instant checkedAt,
        RunActivitySnapshot activity) {

    public static RunRuntimeStatus from(
            Run run,
            RuntimeRun runtime,
            boolean recoveryDispatched) {
        Boolean leaseActive = runtime == null ? null : runtime.executionLeaseActive();
        Boolean recoverable = runtime == null ? null : runtime.recoverable();
        RunState runtimeState = runtime == null ? null : runtime.state();
        RunExecutionStatus executionStatus;
        if (terminal(run.state()) || (runtimeState != null && terminal(runtimeState))) {
            executionStatus = RunExecutionStatus.TERMINAL;
        } else if (run.state() == RunState.WAITING_FOR_INPUT
                || runtimeState == RunState.WAITING_FOR_INPUT) {
            executionStatus = RunExecutionStatus.WAITING_FOR_INPUT;
        } else if (recoveryDispatched) {
            executionStatus = RunExecutionStatus.RECOVERING;
        } else if (Boolean.TRUE.equals(leaseActive)) {
            executionStatus = RunExecutionStatus.ACTIVE;
        } else if (Boolean.TRUE.equals(recoverable)) {
            executionStatus = RunExecutionStatus.RECOVERABLE;
        } else {
            executionStatus = RunExecutionStatus.UNKNOWN;
        }
        Instant checkedAt = runtime == null || runtime.checkedAt() == null
                ? Instant.now()
                : runtime.checkedAt();
        return new RunRuntimeStatus(
                run.runId(),
                run.state(),
                runtimeState,
                executionStatus,
                leaseActive,
                recoverable,
                runtime == null ? null : runtime.leaseExpiresAt(),
                Math.max(
                        run.newestEventId() == null ? 0 : run.newestEventId(),
                        runtime == null || runtime.newestEventId() == null
                                ? 0
                                : runtime.newestEventId()),
                Math.max(run.eventCount(), runtime == null ? 0 : runtime.eventCount()),
                checkedAt,
                RunActivitySnapshot.from(run, runtime, checkedAt));
    }

    public static boolean terminal(RunState state) {
        return state == RunState.COMPLETED
                || state == RunState.FAILED
                || state == RunState.CANCELLED
                || state == RunState.TIMED_OUT;
    }

    public enum RunExecutionStatus {
        ACTIVE,
        RECOVERABLE,
        RECOVERING,
        WAITING_FOR_INPUT,
        TERMINAL,
        UNKNOWN
    }

    public record RunActivitySnapshot(
            String currentRole,
            List<RoleActivityStatus> roleStatuses,
            String currentPhase,
            String pipelineStatus,
            Integer completedSteps,
            Integer totalSteps,
            String currentStep,
            Integer currentStepIndex,
            List<StructuredActivityEvent> recentEvents,
            DeliveryFacts delivery,
            long snapshotCursor,
            Long coverageStartEventId,
            boolean complete,
            Instant checkedAt) {

        public static RunActivitySnapshot from(
                Run run,
                RuntimeRun runtime,
                Instant checkedAt) {
            Map<String, Object> snapshot = nestedMap(
                    runtime == null ? null : runtime.result().get("ui_snapshot"));
            if (!Long.valueOf(1).equals(nonNegativeLong(snapshot.get("schema_version")))) {
                snapshot = Map.of();
            }
            long fallbackCursor = Math.max(
                    run.newestEventId() == null ? 0 : run.newestEventId(),
                    runtime == null || runtime.newestEventId() == null
                            ? 0
                            : runtime.newestEventId());
            Long suppliedCursor = nonNegativeLong(snapshot.get("snapshot_cursor"));
            long snapshotCursor = suppliedCursor == null ? fallbackCursor : suppliedCursor;
            Long coverageStart = positiveLongValue(snapshot.get("coverage_start_event_id"));
            Map<String, Object> pipeline = nestedMap(snapshot.get("pipeline"));
            Integer completedSteps = nonNegativeInteger(pipeline.get("completed_steps"));
            Integer totalSteps = positiveInteger(pipeline.get("total_steps"));
            if (completedSteps != null && totalSteps != null && completedSteps > totalSteps) {
                completedSteps = null;
                totalSteps = null;
            }
            return new RunActivitySnapshot(
                    stableRole(snapshot.get("current_role")),
                    roleStatuses(snapshot.get("role_statuses")),
                    text(snapshot.get("current_phase")),
                    text(pipeline.get("status")),
                    completedSteps,
                    totalSteps,
                    text(pipeline.get("current_step")),
                    nonNegativeInteger(pipeline.get("current_step_index")),
                    recentEvents(snapshot.get("recent_events"), snapshotCursor),
                    DeliveryFacts.from(run, runtime, snapshot.get("delivery")),
                    snapshotCursor,
                    coverageStart,
                    Boolean.TRUE.equals(snapshot.get("coverage_complete")),
                    checkedAt);
        }

        private static List<RoleActivityStatus> roleStatuses(Object value) {
            List<RoleActivityStatus> statuses = new ArrayList<>();
            if (value instanceof Map<?, ?> map) {
                map.forEach((role, status) -> addRoleStatus(statuses, role, status));
            } else if (value instanceof List<?> list) {
                for (Object item : list) {
                    addRoleStatus(statuses, nestedMap(item));
                }
            }
            return List.copyOf(statuses);
        }

        private static void addRoleStatus(
                List<RoleActivityStatus> statuses,
                Object roleValue,
                Object statusValue) {
            String role = stableRole(roleValue);
            String status = text(statusValue);
            if (role != null && status != null) {
                statuses.add(new RoleActivityStatus(role, null, status, null, null));
            }
        }

        private static void addRoleStatus(
                List<RoleActivityStatus> statuses,
                Map<String, Object> entry) {
            String role = stableRole(entry.get("role"));
            String status = text(entry.get("status"));
            if (role != null && status != null) {
                statuses.add(new RoleActivityStatus(
                        role,
                        text(entry.get("label")),
                        status,
                        text(entry.get("phase")),
                        positiveLongValue(entry.get("last_event_id"))));
            }
        }

        private static List<StructuredActivityEvent> recentEvents(Object value, long cursor) {
            if (!(value instanceof List<?> list)) {
                return List.of();
            }
            List<StructuredActivityEvent> events = new ArrayList<>();
            int first = Math.max(0, list.size() - 50);
            for (int index = first; index < list.size(); index++) {
                Map<String, Object> event = nestedMap(list.get(index));
                Long eventId = positiveLongValue(event.get("event_id"));
                if (eventId == null || eventId > cursor) {
                    continue;
                }
                events.add(new StructuredActivityEvent(
                        eventId,
                        text(event.get("kind")),
                        stableRole(event.get("role")),
                        text(event.get("phase")),
                        text(event.get("status")),
                        text(event.get("detail")),
                        instant(event.get("occurred_at")),
                        nonNegativeInteger(event.get("step_index")),
                        positiveInteger(event.get("total_steps"))));
            }
            return List.copyOf(events);
        }

        private static Map<String, Object> nestedMap(Object value) {
            if (!(value instanceof Map<?, ?> raw)) {
                return Map.of();
            }
            Map<String, Object> result = new LinkedHashMap<>();
            raw.forEach((key, item) -> {
                if (key instanceof String text) {
                    result.put(text, item);
                }
            });
            return Collections.unmodifiableMap(result);
        }

        private static Map<String, Object> immutableMap(Object value) {
            return nestedMap(value);
        }

        private static String stableRole(Object value) {
            String role = text(value);
            if ("parts_specialist".equals(role)) {
                role = "parts-specialist";
            } else if ("hardware_engineer".equals(role)) {
                role = "hardware-engineer";
            }
            return role != null && role.matches(
                    "(?:supervisor|architect|parts-specialist|hardware-engineer|reviewer|specialist:[a-z0-9][a-z0-9-]{0,62})")
                    ? role
                    : null;
        }

        private static String text(Object value) {
            return value instanceof String text && !text.isBlank() ? text : null;
        }

        private static Long positiveLongValue(Object value) {
            Long result = nonNegativeLong(value);
            return result != null && result > 0 ? result : null;
        }

        private static Long nonNegativeLong(Object value) {
            if (!(value instanceof Number number)) {
                return null;
            }
            long result = number.longValue();
            return result >= 0 ? result : null;
        }

        private static Integer nonNegativeInteger(Object value) {
            Long result = nonNegativeLong(value);
            return result != null && result <= Integer.MAX_VALUE ? result.intValue() : null;
        }

        private static Integer positiveInteger(Object value) {
            Integer result = nonNegativeInteger(value);
            return result != null && result > 0 ? result : null;
        }

        private static Instant instant(Object value) {
            if (value instanceof Instant instant) {
                return instant;
            }
            if (!(value instanceof String text)) {
                return null;
            }
            try {
                return Instant.parse(text);
            } catch (RuntimeException ignored) {
                return null;
            }
        }
    }

    public record RoleActivityStatus(
            String role,
            String label,
            String status,
            String phase,
            Long lastEventId) {
    }

    public record StructuredActivityEvent(
            long eventId,
            String type,
            String role,
            String phase,
            String status,
            String detail,
            Instant occurredAt,
            Integer stepIndex,
            Integer totalSteps) {
    }

    public record DeliveryFacts(
            String controlState,
            String status,
            String manifestId,
            Integer artifactCount,
            List<Map<String, Object>> artifacts,
            List<String> errors,
            boolean terminal,
            String errorCode,
            String error,
            Instant finishedAt) {

        private static DeliveryFacts from(
                Run run,
                RuntimeRun runtimeRun,
                Object runtimeValue) {
            Map<String, Object> runtime = RunActivitySnapshot.nestedMap(runtimeValue);
            List<Map<String, Object>> artifacts = new ArrayList<>();
            if (runtime.get("artifacts") instanceof List<?> items) {
                for (Object item : items) {
                    Map<String, Object> artifact = RunActivitySnapshot.immutableMap(item);
                    if (!artifact.isEmpty()) {
                        artifacts.add(artifact);
                    }
                }
            }
            List<String> errors = new ArrayList<>();
            if (runtime.get("errors") instanceof List<?> items) {
                for (Object item : items) {
                    String error = RunActivitySnapshot.text(item);
                    if (error != null) {
                        errors.add(error);
                    }
                }
            }
            return new DeliveryFacts(
                    run.state().name(),
                    run.deliveryStatus() == null
                            ? RunActivitySnapshot.text(runtime.get("status"))
                            : run.deliveryStatus().apiValue(),
                    RunActivitySnapshot.text(runtime.get("manifest_id")),
                    RunActivitySnapshot.nonNegativeInteger(runtime.get("artifact_count")),
                    List.copyOf(artifacts),
                    List.copyOf(errors),
                    RunRuntimeStatus.terminal(run.state())
                            || (runtimeRun != null && RunRuntimeStatus.terminal(runtimeRun.state())),
                    run.errorCode() == null && runtimeRun != null
                            ? runtimeRun.errorCode()
                            : run.errorCode(),
                    run.error() == null && runtimeRun != null
                            ? runtimeRun.error()
                            : run.error(),
                    run.finishedAt() == null && runtimeRun != null
                            ? runtimeRun.finishedAt()
                            : run.finishedAt());
        }
    }
}
