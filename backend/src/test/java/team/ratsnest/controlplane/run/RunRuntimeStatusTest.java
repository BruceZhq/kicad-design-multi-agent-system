package team.ratsnest.controlplane.run;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;

import org.junit.jupiter.api.Test;

import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RunState;
import team.ratsnest.controlplane.agentgateway.domain.port.AgentRuntimeGateway.RuntimeRun;
import team.ratsnest.controlplane.run.application.model.RunRuntimeStatus;
import team.ratsnest.controlplane.run.application.model.RunRuntimeStatus.RunExecutionStatus;
import team.ratsnest.controlplane.run.domain.model.Run;

class RunRuntimeStatusTest {

    @Test
    void doesNotTreatDatabaseRunningAsProofOfAnActiveLease() {
        RunRuntimeStatus status = RunRuntimeStatus.from(run(RunState.RUNNING), null, false);

        assertEquals(RunExecutionStatus.UNKNOWN, status.executionStatus());
    }

    @Test
    void exposesRuntimeConfirmedRecoveryEligibility() {
        Run run = run(RunState.RUNNING);
        RuntimeRun runtime = runtimeRun(
                run,
                RunState.RUNNING,
                false,
                true);

        RunRuntimeStatus status = RunRuntimeStatus.from(run, runtime, false);

        assertEquals(RunExecutionStatus.RECOVERABLE, status.executionStatus());
        assertEquals(23, status.lastEventId());
        assertEquals(23, status.eventCount());
    }

    @Test
    void recoveryDispatchIsReportedAsRecovering() {
        Run run = run(RunState.RUNNING);
        RuntimeRun runtime = runtimeRun(
                run,
                RunState.RUNNING,
                true,
                false);

        RunRuntimeStatus status = RunRuntimeStatus.from(run, runtime, true);

        assertEquals(RunExecutionStatus.RECOVERING, status.executionStatus());
    }

    @Test
    void projectsOnlyRuntimeDurableStructuredActivity() {
        Run run = run(RunState.RUNNING);
        Map<String, Object> snapshot = Map.of(
                "schema_version", 1,
                "snapshot_cursor", 23,
                "coverage_start_event_id", 11,
                "coverage_complete", true,
                "current_role", "hardware_engineer",
                "current_phase", "hardware-engineer:route",
                "role_statuses", List.of(Map.of(
                        "role", "hardware_engineer",
                        "label", "Hardware Engineer",
                        "status", "running",
                        "phase", "hardware-engineer:route",
                        "last_event_id", 23)),
                "pipeline", Map.of(
                        "status", "running",
                        "completed_steps", 4,
                        "total_steps", 17,
                        "current_step", "route",
                        "current_step_index", 5),
                "recent_events", List.of(Map.of(
                        "event_id", 23,
                        "kind", "pipeline_step",
                        "role", "hardware_engineer",
                        "phase", "hardware-engineer:route",
                        "status", "running",
                        "detail", "Routing",
                        "step_index", 5,
                        "total_steps", 17)),
                "delivery", Map.of(
                        "status", "in_progress",
                        "artifact_count", 0,
                        "artifacts", List.of(),
                        "errors", List.of()));
        RuntimeRun runtime = runtimeRun(
                run,
                RunState.RUNNING,
                true,
                false,
                Map.of("ui_snapshot", snapshot));

        RunRuntimeStatus status = RunRuntimeStatus.from(run, runtime, false);

        assertEquals("hardware-engineer", status.activity().currentRole());
        assertEquals(4, status.activity().completedSteps());
        assertEquals(17, status.activity().totalSteps());
        assertEquals(23, status.activity().snapshotCursor());
        assertEquals(23, status.activity().recentEvents().getFirst().eventId());
        assertEquals(true, status.activity().complete());
    }

    @Test
    void terminalRunStillReturnsDurableRuntimeActivity() {
        Run run = run(RunState.COMPLETED);
        RuntimeRun runtime = runtimeRun(
                run,
                RunState.COMPLETED,
                false,
                false,
                Map.of("ui_snapshot", Map.of(
                        "schema_version", 1,
                        "snapshot_cursor", 23,
                        "coverage_complete", true,
                        "current_role", "reviewer")));

        RunRuntimeStatus status = RunRuntimeStatus.from(run, runtime, false);

        assertEquals(RunExecutionStatus.TERMINAL, status.executionStatus());
        assertEquals("reviewer", status.activity().currentRole());
        assertEquals(23, status.activity().snapshotCursor());
    }

    private Run run(RunState state) {
        UUID runId = UUID.fromString("3cd14a76-1c4d-4bb8-bbe5-09fc1a754b52");
        return new Run(
                UUID.fromString("9af43a2d-e738-4b63-98eb-e36839ad9f22"),
                runId,
                UUID.fromString("d57ead5c-1751-47aa-b74e-1581edcf3a61"),
                runId,
                null,
                1,
                "thread-1",
                "idempotency-1",
                "fingerprint",
                "message",
                "model",
                Map.of(),
                "profile",
                "1.0",
                "digest",
                "harness",
                "manifest",
                "stable",
                "principal",
                "issuer",
                "subject",
                state,
                null,
                null,
                17,
                1L,
                17L,
                null,
                null,
                Instant.parse("2026-08-20T00:00:00Z"),
                Instant.parse("2026-08-20T00:00:01Z"),
                null);
    }

    private RuntimeRun runtimeRun(
            Run run,
            RunState state,
            boolean leaseActive,
            boolean recoverable) {
        return runtimeRun(run, state, leaseActive, recoverable, Map.of());
    }

    private RuntimeRun runtimeRun(
            Run run,
            RunState state,
            boolean leaseActive,
            boolean recoverable,
            Map<String, Object> result) {
        return new RuntimeRun(
                run.runId().toString(),
                "graph-run",
                "stream",
                state,
                "ratsnestpro",
                run.threadId(),
                run.createdAt(),
                run.startedAt(),
                null,
                23,
                1L,
                23L,
                null,
                null,
                result,
                leaseActive,
                recoverable,
                Instant.parse("2026-08-20T00:05:00Z"),
                Instant.parse("2026-08-20T00:04:00Z"));
    }
}
