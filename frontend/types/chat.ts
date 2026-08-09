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
export type RunState = "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "TIMED_OUT";

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
  return value === "QUEUED" || value === "RUNNING" || value === "COMPLETED" ||
    value === "FAILED" || value === "CANCELLED" || value === "TIMED_OUT";
}

function deliveryStatus(value: unknown): value is DeliveryStatus {
  return value === "execution_blocked" || value === "delivered_with_issues" || value === "release_ready";
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

export function isTerminalRunEvent(value: string): value is TerminalRunEvent {
  return value === "completed" || value === "failed" || value === "cancelled" || value === "timed_out";
}

export function isTerminalReplayFailure(event: RunEvent): boolean {
  return event.type === "error" &&
    event.data.code === "replay_gap" &&
    event.data.retryable === false;
}
