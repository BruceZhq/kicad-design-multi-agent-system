import assert from "node:assert/strict";
import test from "node:test";

import {
  canReconnectRuntimeEvents,
  canRecoverRuntime,
  isRunActivityRoleReached,
  parseRunRuntimeStatus,
  reduceRunActivity,
  runActivityStatusPresentation,
  runtimeStatusFetchMode,
  runtimeStatusLabel,
  selectNewerActivitySnapshot,
} from "../types/chat.ts";

const valid = {
  runId: "a91b4d37-a037-4426-aed7-64b604912aaf",
  controlState: "RUNNING",
  runtimeState: "LEASE_EXPIRED",
  recoveryState: "RECOVERABLE",
  lastEventId: 48,
  eventCount: 48,
  currentPhase: "hardware-engineer",
  completedSteps: 6,
  totalSteps: 17,
  detail: "执行租约已过期",
  checkedAt: "2026-08-20T09:00:00Z",
};

test("parses a recoverable run status", () => {
  assert.deepEqual(parseRunRuntimeStatus(valid), { ...valid, activity: null });
  assert.equal(runtimeStatusLabel("RECOVERABLE"), "执行已中断，可从检查点恢复");
  assert.equal(runtimeStatusLabel("ACTIVE"), "执行器租约在线");
});

test("rejects unknown recovery states and impossible progress", () => {
  assert.equal(parseRunRuntimeStatus({ ...valid, recoveryState: "STALE" }), null);
  assert.equal(parseRunRuntimeStatus({ ...valid, completedSteps: 18 }), null);
});

test("accepts omitted optional progress fields", () => {
  const { currentPhase: _phase, completedSteps: _completed, totalSteps: _total, detail: _detail, ...minimal } = valid;
  assert.deepEqual(parseRunRuntimeStatus(minimal), { ...minimal, activity: null });
});

const activity = {
  currentRole: "hardware-engineer",
  roleStatuses: [{ role: "hardware-engineer", label: "Hardware Engineer", status: "in_progress", phase: "pipeline:pcb", lastEventId: 48 }],
  currentPhase: "pipeline:pcb",
  pipelineStatus: "in_progress",
  completedSteps: 6,
  totalSteps: 17,
  currentStep: "pcb",
  currentStepIndex: 7,
  recentEvents: [{
    eventId: 48,
    type: "workflow_event",
    role: "hardware-engineer",
    phase: "pipeline:pcb",
    status: "in_progress",
    detail: "PCB generation",
    occurredAt: "2026-08-20T09:00:00Z",
    stepIndex: 7,
    totalSteps: 17,
  }],
  delivery: {
    controlState: "RUNNING",
    status: null,
    manifestId: null,
    artifactCount: 1,
    artifacts: [],
    errors: [],
    terminal: false,
    errorCode: null,
    error: null,
    finishedAt: null,
  },
  snapshotCursor: 48,
  coverageStartEventId: 1,
  complete: true,
  checkedAt: "2026-08-20T09:00:00Z",
};

test("parses the authoritative activity snapshot and advances only from its cursor", () => {
  const parsed = parseRunRuntimeStatus({ ...valid, activity });
  assert.deepEqual(parsed?.activity, activity);
  const advanced = reduceRunActivity(parsed?.activity ?? null, {
    eventId: 49,
    runId: valid.runId,
    type: "message",
    createdAt: "2026-08-20T09:00:01Z",
    data: {
      message: {
        type: "custom",
        content: "",
        customData: {
          kind: "workflow_event",
          phase: "pipeline:routing",
          status: "in_progress",
          completed_steps: 7,
          total_steps: 17,
        },
      },
    },
  });
  assert.equal(advanced?.snapshotCursor, 49);
  assert.equal(advanced?.completedSteps, 7);
  assert.equal(advanced?.recentEvents.at(-1)?.phase, "pipeline:routing");
  assert.equal(selectNewerActivitySnapshot(advanced, activity)?.snapshotCursor, 49);
});

test("keeps recovery and event reconnection as separate actions", () => {
  assert.equal(canRecoverRuntime("RECOVERABLE", false), true);
  assert.equal(canRecoverRuntime("ACTIVE", false), false);
  assert.equal(canReconnectRuntimeEvents("ACTIVE", false, "disconnected"), true);
  assert.equal(canReconnectRuntimeEvents("RECOVERABLE", false, "disconnected"), false);
  assert.equal(canReconnectRuntimeEvents("ACTIVE", true, "disconnected"), false);
});

test("uses governed role status meanings", () => {
  assert.deepEqual(runActivityStatusPresentation("running"), { label: "执行中", tone: "live" });
  assert.deepEqual(runActivityStatusPresentation("waiting"), { label: "等待中", tone: "neutral" });
  assert.deepEqual(runActivityStatusPresentation("warning"), { label: "需关注", tone: "attention" });
  assert.deepEqual(runActivityStatusPresentation("execution_blocked"), { label: "有问题", tone: "issue" });
});

test("loads a terminal conversation snapshot once and accepts nullable structured facts", () => {
  const terminalActivity = {
    ...activity,
    currentRole: null,
    currentPhase: null,
    pipelineStatus: "execution_blocked",
    currentStep: "layout_write",
    roleStatuses: [
      { role: "hardware-engineer", label: null, status: "blocked", phase: null, lastEventId: null },
      { role: "reviewer", label: "Reviewer", status: "waiting", phase: null, lastEventId: null },
    ],
    recentEvents: [{
      eventId: 48,
      type: null,
      role: null,
      phase: null,
      status: null,
      detail: null,
      occurredAt: null,
      stepIndex: null,
      totalSteps: null,
    }],
    delivery: {
      ...activity.delivery,
      controlState: "FAILED",
      status: "execution_blocked",
      artifactCount: null,
      terminal: true,
      errors: ["layout_write budget exhausted"],
    },
  };
  const parsed = parseRunRuntimeStatus({
    ...valid,
    controlState: "FAILED",
    runtimeState: "FAILED",
    recoveryState: "TERMINAL",
    activity: terminalActivity,
  });
  assert.equal(runtimeStatusFetchMode("FAILED"), "once");
  assert.equal(runtimeStatusFetchMode("RUNNING"), "poll");
  assert.equal(parsed?.activity?.pipelineStatus, "execution_blocked");
  assert.equal(parsed?.activity?.roleStatuses[0]?.status, "blocked");
  assert.equal(parsed?.activity?.delivery.errors[0], "layout_write budget exhausted");
  assert.equal(isRunActivityRoleReached(parsed!.activity!, "reviewer"), false);
});
