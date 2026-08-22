export interface AgentInfo {
  key: string;
  description: string;
}

export interface CapabilityProfileMetadata {
  id: string;
  version: string;
  digest: string;
  title: string;
  description: string;
}

export interface ServiceInfo {
  agents: AgentInfo[];
  models: string[];
  default_agent: string;
  default_model: string;
  capability_profiles: CapabilityProfileMetadata[];
}

export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  id: string | null;
  type?: "tool_call";
}

export type MessageType = "human" | "ai" | "tool" | "custom";

export interface ChatMessage {
  type: MessageType;
  content: string;
  tool_calls: ToolCall[];
  tool_call_id: string | null;
  run_id: string | null;
  response_metadata: Record<string, unknown>;
  custom_data: Record<string, unknown>;
}

export interface DisplayMessage extends ChatMessage {
  clientId: string;
  pending?: boolean;
  reasoning?: string;
}

export type TerminalRunEvent = "completed" | "failed" | "cancelled" | "timed_out";
export type DeliveryStatus = "execution_blocked" | "delivered_with_issues" | "release_ready";
export type RunState = "QUEUED" | "RUNNING" | "WAITING_FOR_INPUT" | "COMPLETED" | "FAILED" | "CANCELLED" | "TIMED_OUT";
export type RunRecoveryState = "ACTIVE" | "RECOVERABLE" | "RECOVERING" | "WAITING_FOR_INPUT" | "TERMINAL" | "UNKNOWN";
export type RunEventConnectionState = "idle" | "connecting" | "connected" | "retrying" | "disconnected";

export interface RunRuntimeStatus {
  runId: string;
  controlState: RunState;
  runtimeState: string;
  recoveryState: RunRecoveryState;
  lastEventId: number;
  eventCount: number;
  currentPhase?: string;
  completedSteps?: number;
  totalSteps?: number;
  detail?: string;
  checkedAt: string;
  activity: RunActivitySnapshot | null;
}

export interface RunActivityRoleStatus {
  role: string;
  label: string | null;
  status: string;
  phase: string | null;
  lastEventId: number | null;
}

export interface RunActivityEvent {
  eventId: number;
  type: string | null;
  role: string | null;
  phase: string | null;
  status: string | null;
  detail: string | null;
  occurredAt: string | null;
  stepIndex: number | null;
  totalSteps: number | null;
}

export interface RunDeliverySnapshot {
  controlState: string;
  status: string | null;
  manifestId: string | null;
  artifactCount: number | null;
  artifacts: unknown[];
  errors: unknown[];
  terminal: boolean;
  errorCode: string | null;
  error: string | null;
  finishedAt: string | null;
}

export interface RunActivitySnapshot {
  snapshotCursor: number;
  coverageStartEventId: number | null;
  complete: boolean;
  checkedAt: string;
  currentRole: string | null;
  currentPhase: string | null;
  roleStatuses: RunActivityRoleStatus[];
  pipelineStatus: string | null;
  completedSteps: number | null;
  totalSteps: number | null;
  currentStep: string | null;
  currentStepIndex: number | null;
  recentEvents: RunActivityEvent[];
  delivery: RunDeliverySnapshot;
}

export interface HumanDecisionOption {
  key: string;
  label: string;
  basis: string;
  freeText: boolean;
}

export interface HumanDecisionQuestion {
  slot: string;
  question: string;
  kind: string;
  recommendedKey: string;
  citation: string;
  options: HumanDecisionOption[];
}

export interface HumanInputRequest {
  interactionId: string;
  kind: "clarification";
  question: string;
  options: string[];
  allowFreeText: boolean;
  requestedBy: string;
  stateVersion: number;
  schemaVersion: string | null;
  questions: HumanDecisionQuestion[];
}

export interface RunSummary {
  runId: string;
  rootRunId: string;
  parentRunId: string | null;
  revisionNumber: number;
  projectId: string;
  threadId: string;
  capabilityProfile: {
    id: string;
    version: string;
    digest: string;
  } | null;
  state: RunState;
  deliveryStatus: DeliveryStatus | null;
}

export interface ConversationSummary {
  threadId: string;
  title: string;
  latestRunId: string;
  latestRevisionNumber: number;
  state: RunState;
  deliveryStatus: DeliveryStatus | null;
  lastEventId: number;
  pendingInteraction: HumanInputRequest | null;
  createdAt: string;
  updatedAt: string;
}

export interface ConversationListResponse {
  conversations: ConversationSummary[];
}

export interface RunArtifact {
  artifactId: string;
  runId: string;
  fileName: string;
  kind: string;
  mediaType: string;
  sizeBytes: number;
  sha256: string;
  createdAt: string;
}

export interface ArtifactListResponse {
  runId: string;
  superseded: boolean;
  artifacts: RunArtifact[];
}

export interface RunEvent {
  eventId: number;
  runId: string;
  type: string;
  createdAt: string;
  data: Record<string, unknown>;
}

export function makeMessage(
  type: MessageType,
  content: string,
  extra: Partial<ChatMessage> = {},
): ChatMessage {
  return {
    type,
    content,
    tool_calls: [],
    tool_call_id: null,
    run_id: null,
    response_metadata: {},
    custom_data: {},
    ...extra,
  };
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function uuid(value: unknown): value is string {
  return typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function runState(value: unknown): value is RunState {
  return value === "QUEUED" || value === "RUNNING" || value === "WAITING_FOR_INPUT" || value === "COMPLETED" ||
    value === "FAILED" || value === "CANCELLED" || value === "TIMED_OUT";
}

function deliveryStatus(value: unknown): value is DeliveryStatus {
  return value === "execution_blocked" || value === "delivered_with_issues" || value === "release_ready";
}

export function parseConversationList(value: unknown): ConversationListResponse | null {
  const body = record(value);
  if (!Array.isArray(body.conversations)) return null;
  const conversations: ConversationSummary[] = [];
  for (const value of body.conversations) {
    const item = record(value);
    const pendingInteraction = item.pendingInteraction === null
      ? null
      : parseHumanInputRequest({
          eventId: 1,
          runId: typeof item.latestRunId === "string" ? item.latestRunId : "",
          type: "ag_ui",
          createdAt: typeof item.updatedAt === "string" ? item.updatedAt : "",
          data: {
            agUi: {
              type: "CUSTOM",
              name: "ratsnest.human-input-required.v1",
              value: item.pendingInteraction,
            },
          },
        });
    if (
      typeof item.threadId !== "string" ||
      !/^[A-Za-z0-9._:-]{1,200}$/.test(item.threadId) ||
      typeof item.title !== "string" ||
      item.title.length < 1 ||
      item.title.length > 81 ||
      !uuid(item.latestRunId) ||
      !Number.isSafeInteger(item.latestRevisionNumber) ||
      Number(item.latestRevisionNumber) < 1 ||
      !runState(item.state) ||
      (item.deliveryStatus !== null && !deliveryStatus(item.deliveryStatus)) ||
      !Number.isSafeInteger(item.lastEventId) ||
      Number(item.lastEventId) < 0 ||
      (item.pendingInteraction !== null && pendingInteraction === null) ||
      typeof item.createdAt !== "string" ||
      !Number.isFinite(Date.parse(item.createdAt)) ||
      typeof item.updatedAt !== "string" ||
      !Number.isFinite(Date.parse(item.updatedAt))
    ) return null;
    conversations.push({
      threadId: item.threadId,
      title: item.title,
      latestRunId: item.latestRunId,
      latestRevisionNumber: Number(item.latestRevisionNumber),
      state: item.state,
      deliveryStatus: item.deliveryStatus,
      lastEventId: Number(item.lastEventId),
      pendingInteraction,
      createdAt: item.createdAt,
      updatedAt: item.updatedAt,
    });
  }
  return { conversations };
}

export function parseRunSummary(value: unknown): RunSummary | null {
  const item = record(value);
  const capabilityProfile = item.capabilityProfile === null || item.capabilityProfile === undefined
    ? null
    : parseCapabilityProfileSnapshot(item.capabilityProfile);
  if (
    !uuid(item.runId) ||
    !uuid(item.rootRunId) ||
    (item.parentRunId !== null && !uuid(item.parentRunId)) ||
    !Number.isSafeInteger(item.revisionNumber) ||
    Number(item.revisionNumber) < 1 ||
    !uuid(item.projectId) ||
    typeof item.threadId !== "string" ||
    !/^[A-Za-z0-9._:-]{1,200}$/.test(item.threadId) ||
    (item.capabilityProfile !== null && item.capabilityProfile !== undefined && capabilityProfile === null) ||
    !runState(item.state) ||
    (item.deliveryStatus !== null && !deliveryStatus(item.deliveryStatus))
  ) return null;
  return {
    runId: item.runId,
    rootRunId: item.rootRunId,
    parentRunId: item.parentRunId,
    revisionNumber: Number(item.revisionNumber),
    projectId: item.projectId,
    threadId: item.threadId,
    capabilityProfile,
    state: item.state,
    deliveryStatus: item.deliveryStatus,
  };
}

function nullableText(value: unknown, maximum = 500): string | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  return typeof value === "string" && value.length <= maximum ? value : undefined;
}

function nullableCount(value: unknown): number | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : undefined;
}

function parseRunActivitySnapshot(value: unknown): RunActivitySnapshot | null {
  if (value === null || value === undefined) return null;
  const item = record(value);
  const coverageStart = nullableCount(item.coverageStartEventId);
  const currentRole = nullableText(item.currentRole, 200);
  const currentPhase = nullableText(item.currentPhase, 200);
  const pipelineStatus = nullableText(item.pipelineStatus, 100);
  const completedSteps = nullableCount(item.completedSteps);
  const totalSteps = nullableCount(item.totalSteps);
  const currentStep = nullableText(item.currentStep, 200);
  const currentStepIndex = nullableCount(item.currentStepIndex);
  if (
    !Number.isSafeInteger(item.snapshotCursor) || Number(item.snapshotCursor) < 0 ||
    coverageStart === undefined || typeof item.complete !== "boolean" ||
    typeof item.checkedAt !== "string" || !Number.isFinite(Date.parse(item.checkedAt)) ||
    currentRole === undefined || currentPhase === undefined || pipelineStatus === undefined ||
    completedSteps === undefined || totalSteps === undefined || currentStep === undefined || currentStepIndex === undefined ||
    !Array.isArray(item.roleStatuses) || item.roleStatuses.length > 64 ||
    !Array.isArray(item.recentEvents) || item.recentEvents.length > 100
  ) return null;

  const roleStatuses: RunActivityRoleStatus[] = [];
  for (const raw of item.roleStatuses) {
    const role = record(raw);
    const label = nullableText(role.label, 200);
    const phase = nullableText(role.phase, 200);
    const lastEventId = nullableCount(role.lastEventId);
    if (
      typeof role.role !== "string" || role.role.length < 1 || role.role.length > 200 ||
      typeof role.status !== "string" || role.status.length < 1 || role.status.length > 100 ||
      label === undefined || phase === undefined || lastEventId === undefined
    ) return null;
    roleStatuses.push({
      role: role.role,
      label: label ?? null,
      status: role.status,
      phase: phase ?? null,
      lastEventId,
    });
  }

  const recentEvents: RunActivityEvent[] = [];
  for (const raw of item.recentEvents) {
    const event = record(raw);
    const type = nullableText(event.type, 100);
    const role = nullableText(event.role, 200);
    const phase = nullableText(event.phase, 200);
    const status = nullableText(event.status, 100);
    const detail = nullableText(event.detail, 2_000);
    const occurredAt = nullableText(event.occurredAt, 100);
    const stepIndex = nullableCount(event.stepIndex);
    const totalSteps = nullableCount(event.totalSteps);
    if (
      !Number.isSafeInteger(event.eventId) || Number(event.eventId) < 1 ||
      type === undefined || role === undefined || phase === undefined || status === undefined || detail === undefined ||
      occurredAt === undefined || (occurredAt !== null && !Number.isFinite(Date.parse(occurredAt))) ||
      stepIndex === undefined || totalSteps === undefined ||
      Number(event.eventId) > Number(item.snapshotCursor)
    ) return null;
    recentEvents.push({
      eventId: Number(event.eventId),
      type: type ?? null,
      role: role ?? null,
      phase: phase ?? null,
      status: status ?? null,
      detail: detail ?? null,
      occurredAt: occurredAt ?? null,
      stepIndex,
      totalSteps,
    });
  }

  const deliveryItem = record(item.delivery);
  const deliveryStatus = nullableText(deliveryItem.status, 100);
  const manifestId = nullableText(deliveryItem.manifestId, 200);
  const errorCode = nullableText(deliveryItem.errorCode, 200);
  const error = nullableText(deliveryItem.error, 2_000);
  const finishedAt = nullableText(deliveryItem.finishedAt, 100);
  const artifactCount = nullableCount(deliveryItem.artifactCount);
  if (
    typeof deliveryItem.controlState !== "string" || deliveryItem.controlState.length > 100 ||
    deliveryStatus === undefined || manifestId === undefined || errorCode === undefined || error === undefined ||
    finishedAt === undefined || (finishedAt !== null && !Number.isFinite(Date.parse(finishedAt))) ||
    artifactCount === undefined ||
    !Array.isArray(deliveryItem.artifacts) || !Array.isArray(deliveryItem.errors) ||
    typeof deliveryItem.terminal !== "boolean"
  ) return null;
  const delivery: RunDeliverySnapshot = {
    controlState: deliveryItem.controlState,
    status: deliveryStatus,
    manifestId,
    artifactCount,
    artifacts: deliveryItem.artifacts,
    errors: deliveryItem.errors,
    terminal: deliveryItem.terminal,
    errorCode,
    error,
    finishedAt,
  };

  return {
    snapshotCursor: Number(item.snapshotCursor),
    coverageStartEventId: coverageStart,
    complete: item.complete,
    checkedAt: item.checkedAt,
    currentRole: currentRole ?? null,
    currentPhase: currentPhase ?? null,
    roleStatuses,
    pipelineStatus: pipelineStatus ?? null,
    completedSteps,
    totalSteps,
    currentStep: currentStep ?? null,
    currentStepIndex,
    recentEvents: recentEvents.sort((left, right) => left.eventId - right.eventId),
    delivery,
  };
}

export function parseRunRuntimeStatus(value: unknown): RunRuntimeStatus | null {
  const item = record(value);
  const recoveryState = item.recoveryState;
  const currentPhase = item.currentPhase;
  const completedSteps = item.completedSteps;
  const totalSteps = item.totalSteps;
  const detail = item.detail;
  const rawActivity = item.activity ?? item.uiSnapshot;
  const activity = parseRunActivitySnapshot(rawActivity);
  if (
    !uuid(item.runId) ||
    !runState(item.controlState) ||
    typeof item.runtimeState !== "string" ||
    item.runtimeState.length < 1 ||
    item.runtimeState.length > 100 ||
    (recoveryState !== "ACTIVE" && recoveryState !== "RECOVERABLE" &&
      recoveryState !== "RECOVERING" && recoveryState !== "WAITING_FOR_INPUT" &&
      recoveryState !== "TERMINAL" && recoveryState !== "UNKNOWN") ||
    !Number.isSafeInteger(item.lastEventId) || Number(item.lastEventId) < 0 ||
    !Number.isSafeInteger(item.eventCount) || Number(item.eventCount) < 0 ||
    (currentPhase !== undefined && currentPhase !== null &&
      (typeof currentPhase !== "string" || currentPhase.length > 200)) ||
    (completedSteps !== undefined && completedSteps !== null &&
      (!Number.isSafeInteger(completedSteps) || Number(completedSteps) < 0)) ||
    (totalSteps !== undefined && totalSteps !== null &&
      (!Number.isSafeInteger(totalSteps) || Number(totalSteps) < 0)) ||
    (detail !== undefined && detail !== null &&
      (typeof detail !== "string" || detail.length > 2_000)) ||
    typeof item.checkedAt !== "string" || !Number.isFinite(Date.parse(item.checkedAt)) ||
    (rawActivity !== undefined && rawActivity !== null && activity === null)
  ) return null;
  if (completedSteps != null && totalSteps != null && Number(completedSteps) > Number(totalSteps)) return null;
  return {
    runId: item.runId,
    controlState: item.controlState,
    runtimeState: item.runtimeState,
    recoveryState,
    lastEventId: Number(item.lastEventId),
    eventCount: Number(item.eventCount),
    ...(typeof currentPhase === "string" ? { currentPhase } : {}),
    ...(completedSteps != null ? { completedSteps: Number(completedSteps) } : {}),
    ...(totalSteps != null ? { totalSteps: Number(totalSteps) } : {}),
    ...(typeof detail === "string" ? { detail } : {}),
    checkedAt: item.checkedAt,
    activity,
  };
}

export function runtimeStatusLabel(state: RunRecoveryState): string {
  if (state === "ACTIVE") return "执行器租约在线";
  if (state === "RECOVERABLE") return "执行已中断，可从检查点恢复";
  if (state === "RECOVERING") return "正在恢复";
  if (state === "WAITING_FOR_INPUT") return "等待你的确认";
  if (state === "TERMINAL") return "任务已结束";
  return "正在确认运行状态";
}

export function runtimeStatusFetchMode(state: RunState): "poll" | "once" {
  return state === "QUEUED" || state === "RUNNING" || state === "WAITING_FOR_INPUT" ? "poll" : "once";
}

export type RunActivityStatusTone = "live" | "attention" | "issue" | "neutral";

/** Translate the backend's governed public status vocabulary without guessing from chat text. */
export function runActivityStatusPresentation(status: string | null | undefined): {
  label: string;
  tone: RunActivityStatusTone;
} {
  const value = status?.trim().toLowerCase() ?? "";
  if (value === "running" || value === "started" || value === "retrying" || value === "in_progress") {
    return { label: "执行中", tone: "live" };
  }
  if (value === "waiting" || value === "waiting_for_input" || value === "queued") {
    return { label: "等待中", tone: "neutral" };
  }
  if (value === "completed" || value === "ok" || value === "release_ready") {
    return { label: "已完成", tone: "neutral" };
  }
  if (value === "warning" || value === "partial" || value === "unavailable" || value === "delivered_with_issues") {
    return { label: "需关注", tone: "attention" };
  }
  if (value === "blocked" || value === "failed" || value === "error" || value === "execution_blocked") {
    return { label: "有问题", tone: "issue" };
  }
  return { label: "未知", tone: "neutral" };
}

/** A waiting placeholder without an event cursor is not evidence that the role was reached. */
export function isRunActivityRoleReached(activity: RunActivitySnapshot, role: string): boolean {
  const item = activity.roleStatuses.find((candidate) => candidate.role === role);
  if (!item) return false;
  const status = item.status.trim().toLowerCase();
  return item.lastEventId !== null || (status !== "waiting" && status !== "queued");
}

export function canRecoverRuntime(state: RunRecoveryState, stale: boolean): boolean {
  return !stale && state === "RECOVERABLE";
}

export function canReconnectRuntimeEvents(
  state: RunRecoveryState,
  stale: boolean,
  connection: RunEventConnectionState,
): boolean {
  return !stale && state === "ACTIVE" && connection === "disconnected";
}

export function selectNewerActivitySnapshot(
  current: RunActivitySnapshot | null,
  incoming: RunActivitySnapshot | null,
): RunActivitySnapshot | null {
  if (incoming === null) return null;
  if (current !== null && current.snapshotCursor > incoming.snapshotCursor) return current;
  return incoming;
}

function activityRole(phase: string, supplied: unknown): string {
  if (typeof supplied === "string" && supplied.length > 0 && supplied.length <= 200) return supplied;
  if (phase === "intent-router" || phase === "supervisor") return "supervisor";
  if (phase === "architect" || phase === "parts-specialist" || phase === "reviewer") return phase;
  if (phase.startsWith("hardware-engineer") || phase.startsWith("pipeline:")) return "hardware-engineer";
  if (phase.startsWith("specialist:")) return phase;
  return "";
}

/** Apply one authoritative live event without inserting it into chat history. */
export function reduceRunActivity(
  snapshot: RunActivitySnapshot | null,
  event: RunEvent,
): RunActivitySnapshot | null {
  if (snapshot === null || event.eventId <= snapshot.snapshotCursor) return snapshot;
  const cursorOnly = { ...snapshot, snapshotCursor: event.eventId };
  if (event.type !== "message") return cursorOnly;
  const message = parseChatMessage(event.data.message);
  if (!message || message.type !== "custom") return cursorOnly;
  const custom = record(message.custom_data);
  if (custom.kind !== "workflow_event") return cursorOnly;
  const phase = typeof custom.phase === "string" ? custom.phase.slice(0, 200) : "";
  const status = typeof custom.status === "string" ? custom.status.slice(0, 100) : "";
  if (!phase || !status) return cursorOnly;
  const role = activityRole(phase, custom.role);
  const detail = typeof custom.detail === "string" ? custom.detail.slice(0, 2_000) : "";
  const completed = nullableCount(custom.completedSteps ?? custom.completed_steps);
  const total = nullableCount(custom.totalSteps ?? custom.total_steps);
  const stepIndex = nullableCount(custom.stepIndex ?? custom.step_index);
  const previousRole = snapshot.roleStatuses.find((item) => item.role === role);
  const roleStatuses = role
    ? [
        ...snapshot.roleStatuses.filter((item) => item.role !== role),
        {
          role,
          label: previousRole?.label ?? role,
          status,
          phase,
          lastEventId: event.eventId,
        },
      ]
    : snapshot.roleStatuses;
  const milestone: RunActivityEvent = {
    eventId: event.eventId,
    type: "workflow_event",
    role,
    phase,
    status,
    detail,
    occurredAt: event.createdAt,
    stepIndex: stepIndex ?? null,
    totalSteps: total ?? null,
  };
  const recentEvents = [...snapshot.recentEvents.filter((item) => item.eventId !== event.eventId), milestone]
    .sort((left, right) => left.eventId - right.eventId)
    .slice(-20);
  const pipelineRelevant = phase.startsWith("pipeline:") || completed !== undefined || total !== undefined;
  return {
    ...snapshot,
    snapshotCursor: event.eventId,
    currentRole: role || snapshot.currentRole,
    currentPhase: phase,
    roleStatuses,
    pipelineStatus: pipelineRelevant ? status : snapshot.pipelineStatus,
    completedSteps: pipelineRelevant && completed !== undefined ? completed : snapshot.completedSteps,
    totalSteps: pipelineRelevant && total !== undefined ? total : snapshot.totalSteps,
    currentStep: phase.startsWith("pipeline:") ? phase.slice("pipeline:".length) : snapshot.currentStep,
    currentStepIndex: pipelineRelevant && stepIndex !== undefined ? stepIndex : snapshot.currentStepIndex,
    recentEvents,
  };
}

function parseCapabilityProfileSnapshot(value: unknown): RunSummary["capabilityProfile"] {
  const item = record(value);
  if (
    typeof item.id !== "string" ||
    !/^[a-z0-9][a-z0-9-]{1,63}$/.test(item.id) ||
    typeof item.version !== "string" ||
    !/^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))?$/.test(item.version) ||
    typeof item.digest !== "string" ||
    !/^[0-9a-f]{64}$/.test(item.digest)
  ) return null;
  return { id: item.id, version: item.version, digest: item.digest };
}

function parseRunArtifact(value: unknown): RunArtifact | null {
  const item = record(value);
  if (
    !uuid(item.artifactId) ||
    !uuid(item.runId) ||
    typeof item.fileName !== "string" ||
    item.fileName.length < 1 ||
    item.fileName.length > 255 ||
    typeof item.kind !== "string" ||
    !/^[a-z0-9][a-z0-9._-]{0,79}$/.test(item.kind) ||
    typeof item.mediaType !== "string" ||
    item.mediaType.length < 1 ||
    item.mediaType.length > 200 ||
    !Number.isSafeInteger(item.sizeBytes) ||
    Number(item.sizeBytes) < 1 ||
    typeof item.sha256 !== "string" ||
    !/^[0-9a-f]{64}$/.test(item.sha256) ||
    typeof item.createdAt !== "string" ||
    !Number.isFinite(Date.parse(item.createdAt))
  ) return null;
  return {
    artifactId: item.artifactId,
    runId: item.runId,
    fileName: item.fileName,
    kind: item.kind,
    mediaType: item.mediaType,
    sizeBytes: Number(item.sizeBytes),
    sha256: item.sha256,
    createdAt: item.createdAt,
  };
}

export function parseArtifactList(value: unknown): ArtifactListResponse | null {
  const item = record(value);
  if (!uuid(item.runId) || typeof item.superseded !== "boolean" || !Array.isArray(item.artifacts)) return null;
  const artifacts = item.artifacts.map(parseRunArtifact);
  if (artifacts.some((artifact) => artifact === null)) return null;
  const parsed = artifacts as RunArtifact[];
  if (parsed.some((artifact) => artifact.runId !== item.runId)) return null;
  return { runId: item.runId, superseded: item.superseded, artifacts: parsed };
}

export function parseCapabilityProfile(value: unknown): CapabilityProfileMetadata | null {
  const item = record(value);
  if (
    typeof item.id !== "string" ||
    !/^[a-z0-9][a-z0-9-]{1,63}$/.test(item.id) ||
    typeof item.version !== "string" ||
    item.version.length > 32 ||
    !/^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))?$/.test(item.version) ||
    typeof item.digest !== "string" ||
    !/^[0-9a-f]{64}$/.test(item.digest) ||
    typeof item.title !== "string" ||
    item.title.length < 1 ||
    item.title.length > 120 ||
    typeof item.description !== "string" ||
    item.description.length < 1 ||
    item.description.length > 500
  ) return null;
  return {
    id: item.id,
    version: item.version,
    digest: item.digest,
    title: item.title,
    description: item.description,
  };
}

function toolCall(value: unknown): ToolCall | null {
  const item = record(value);
  if (typeof item.name !== "string") return null;
  return {
    name: item.name,
    args: record(item.args),
    id: typeof item.id === "string" ? item.id : null,
    type: "tool_call",
  };
}

export function parseChatMessage(value: unknown): ChatMessage | null {
  const item = record(value);
  if (
    item.type !== "human" &&
    item.type !== "ai" &&
    item.type !== "tool" &&
    item.type !== "custom"
  ) return null;
  if (typeof item.content !== "string") return null;
  const rawToolCalls = item.toolCalls ?? item.tool_calls;
  const parsedTools = Array.isArray(rawToolCalls)
    ? rawToolCalls.map(toolCall).filter((tool): tool is ToolCall => tool !== null)
    : [];
  const toolCallId = item.toolCallId ?? item.tool_call_id;
  const runId = item.runId ?? item.run_id;
  return makeMessage(item.type, item.content, {
    tool_calls: parsedTools,
    tool_call_id: typeof toolCallId === "string" ? toolCallId : null,
    run_id: typeof runId === "string" ? runId : null,
    response_metadata: record(item.responseMetadata ?? item.response_metadata),
    custom_data: record(item.customData ?? item.custom_data),
  });
}

export function parseRunEvent(value: unknown): RunEvent | null {
  const item = record(value);
  if (
    typeof item.eventId !== "number" ||
    !Number.isSafeInteger(item.eventId) ||
    item.eventId < 1 ||
    typeof item.runId !== "string" ||
    typeof item.type !== "string" ||
    typeof item.createdAt !== "string" ||
    !item.data ||
    typeof item.data !== "object" ||
    Array.isArray(item.data)
  ) return null;
  return {
    eventId: Number(item.eventId),
    runId: item.runId,
    type: item.type,
    createdAt: item.createdAt,
    data: item.data as Record<string, unknown>,
  };
}

function objectOrJson(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string" || value.length > 100_000) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** Extract the one supported HITL contract without claiming unrelated CUSTOM events. */
export function parseHumanInputRequest(event: RunEvent): HumanInputRequest | null {
  const queue: Array<{ value: unknown; depth: number }> = [{ value: event.data, depth: 0 }];
  const seen = new Set<object>();
  let agUiEvent: Record<string, unknown> | null = null;

  while (queue.length > 0) {
    const current = queue.shift()!;
    const item = objectOrJson(current.value);
    if (!item || seen.has(item)) continue;
    seen.add(item);
    if (item.type === "CUSTOM" && item.name === "ratsnest.human-input-required.v1") {
      agUiEvent = item;
      break;
    }
    if (current.depth >= 3) continue;
    for (const key of ["agUi", "ag_ui", "content", "payload", "event"]) {
      if (item[key] !== undefined) queue.push({ value: item[key], depth: current.depth + 1 });
    }
  }

  const value = objectOrJson(agUiEvent?.value);
  if (!value) return null;
  const options = Array.isArray(value.options) && value.options.every(
    (option) => typeof option === "string" && option.trim().length > 0 && option.length <= 500,
  )
    ? value.options.map((option) => String(option).trim())
    : null;
  const questions = parseHumanDecisionQuestions(value.questions);
  if (
    typeof value.interactionId !== "string" ||
    !/^[A-Za-z0-9._:-]{1,200}$/.test(value.interactionId) ||
    value.kind !== "clarification" ||
    typeof value.question !== "string" ||
    value.question.trim().length < 1 ||
    value.question.length > 10_000 ||
    options === null ||
    options.length > 20 ||
    questions === null ||
    (questions.length > 0 && value.schemaVersion !== "ratsnest.decision-request.v1") ||
    typeof value.allowFreeText !== "boolean" ||
    typeof value.requestedBy !== "string" ||
    value.requestedBy.trim().length < 1 ||
    value.requestedBy.length > 100 ||
    !Number.isSafeInteger(value.stateVersion) ||
    Number(value.stateVersion) < 1 ||
    (!value.allowFreeText && options.length === 0)
  ) return null;
  return {
    interactionId: value.interactionId,
    kind: "clarification",
    question: value.question.trim(),
    options,
    allowFreeText: value.allowFreeText,
    requestedBy: value.requestedBy.trim(),
    stateVersion: Number(value.stateVersion),
    schemaVersion: typeof value.schemaVersion === "string" ? value.schemaVersion : null,
    questions,
  };
}

function parseHumanDecisionQuestions(value: unknown): HumanDecisionQuestion[] | null {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length < 1 || value.length > 12) return null;
  const questions: HumanDecisionQuestion[] = [];
  const slots = new Set<string>();
  for (const rawQuestion of value) {
    const question = objectOrJson(rawQuestion);
    if (!question ||
      typeof question.slot !== "string" ||
      !/^[a-z][a-z0-9_]{0,63}$/.test(question.slot) ||
      slots.has(question.slot) ||
      typeof question.question !== "string" ||
      question.question.trim().length < 1 ||
      question.question.length > 1_000 ||
      typeof question.kind !== "string" ||
      question.kind.length > 40 ||
      typeof question.recommendedKey !== "string" ||
      question.recommendedKey.length > 16 ||
      typeof question.citation !== "string" ||
      question.citation.length > 500 ||
      !Array.isArray(question.options) ||
      question.options.length < 2 ||
      question.options.length > 6
    ) return null;
    const decisionOptions: HumanDecisionOption[] = [];
    const keys = new Set<string>();
    for (const rawOption of question.options) {
      const option = objectOrJson(rawOption);
      if (!option ||
        typeof option.key !== "string" ||
        !/^[A-Z][A-Z0-9_.+-]{0,15}$/.test(option.key) ||
        keys.has(option.key) ||
        typeof option.label !== "string" ||
        option.label.trim().length < 1 ||
        option.label.length > 500 ||
        typeof option.basis !== "string" ||
        option.basis.length > 500 ||
        typeof option.freeText !== "boolean"
      ) return null;
      keys.add(option.key);
      decisionOptions.push({
        key: option.key,
        label: option.label.trim(),
        basis: option.basis.trim(),
        freeText: option.freeText,
      });
    }
    if (question.recommendedKey && !keys.has(question.recommendedKey)) return null;
    slots.add(question.slot);
    questions.push({
      slot: question.slot,
      question: question.question.trim(),
      kind: question.kind,
      recommendedKey: question.recommendedKey,
      citation: question.citation.trim(),
      options: decisionOptions,
    });
  }
  return questions;
}

export function isTerminalRunEvent(value: string): value is TerminalRunEvent {
  return value === "completed" || value === "failed" || value === "cancelled" || value === "timed_out";
}

export function isTerminalReplayFailure(event: RunEvent): boolean {
  return event.type === "error" &&
    event.data.code === "replay_gap" &&
    event.data.retryable === false;
}
