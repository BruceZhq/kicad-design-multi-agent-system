export type DesignBackend = "template" | "crew" | "mcp";

export interface HealthResponse {
  service: string;
  status: string;
}

export interface AuthResult {
  token?: string;
  tokenType?: string;
  username?: string;
  role?: string;
  organizationId?: string;
  workspaceId?: string;
  projectId?: string;
  error?: string;
}

export interface CreateRunResponse {
  runId: string;
  status: string;
  backend?: string;
  projectDir?: string;
  projectId?: string;
}

export interface DesignRun {
  id: string;
  kind?: string | null;
  backend?: string | null;
  owner?: string | null;
  ownerUserId?: string | null;
  organizationId?: string | null;
  workspaceId?: string | null;
  projectId?: string | null;
  status: string;
  projectDir?: string | null;
  requirement?: string | null;
  maxIterations?: number | null;
  pythonRunId?: string | null;
  strategyVersionId?: string | null;
  initialScore?: number | null;
  finalScore?: number | null;
  resultJson?: string | null;
  createdAt?: string | null;
  finishedAt?: string | null;
  updatedAt?: string | null;
  startedAt?: string | null;
  attempt?: number;
  failureMessage?: string | null;
  releaseStatus?: "draft" | "review_pending" | "approved" | "rejected" | "blocked" | null;
  dispatchPhase?: "plan" | "execute" | null;
  planContractVersion?: string | null;
  planSha256?: string | null;
  planCreatedAt?: string | null;
  planApprovedAt?: string | null;
}

export interface TenantProject {
  id: string;
  name: string;
  description?: string | null;
}

export interface TenantWorkspace {
  id: string;
  name: string;
  projects: TenantProject[];
}

export interface TenantOrganization {
  id: string;
  name: string;
  slug: string;
  role: string;
  workspaces: TenantWorkspace[];
}

export interface TenantContext {
  userId: string;
  username: string;
  organizations: TenantOrganization[];
}

export interface RunApproval {
  id: string;
  runId: string;
  type: string;
  status: "pending" | "approved" | "rejected";
  subjectSha256: string;
  requestedAt: string;
  decidedAt?: string | null;
  decidedBy?: string | null;
  comment?: string | null;
}

export interface BoardPlanComponent {
  ref: string;
  symbol: string;
  value: string;
  footprint?: string;
  catalog_id?: string;
  role?: string;
  properties?: Record<string, string>;
}

export interface BoardPlan {
  plan_id?: string;
  topology: string;
  components: BoardPlanComponent[];
  connections?: unknown[];
  constraints?: string[];
  rationale?: string;
  outline?: { width: number; height: number };
  family_version?: string;
  catalog_version?: string;
  required_gates?: string[];
  design_limits?: Record<string, number | null>;
}

export type GateStatus = "passed" | "failed" | "unavailable" | "error";

export interface VerificationGate {
  name: string;
  status: GateStatus;
  required: boolean;
  summary: string;
  tool: string;
  evidence: string[];
  metrics: Record<string, unknown>;
}

export interface DesignPlan {
  contractVersion: string;
  runId: string;
  requirement: string;
  backend: DesignBackend;
  strategyName: string;
  strategyVersionId: string;
  subjectSha256: string;
  createdAt: string;
  designSpec: {
    project_name?: string;
    input_voltage?: number;
    output_voltage?: number;
    output_current_a?: number;
    led?: string | null;
  };
  boardPlan: BoardPlan;
}

export interface PatchPlan {
  ops?: unknown[];
  rationale?: Record<string, string>;
}

export interface RunIteration {
  iteration: number;
  score_delta?: number | null;
  scorecard: {
    score?: number | null;
    required_gates_passed?: boolean;
    gate_results?: Record<string, VerificationGate>;
    [key: string]: unknown;
  };
  patch_plan?: PatchPlan | null;
  resolved_findings?: string[];
}

export interface RunRecord {
  run_id?: string;
  status?: string;
  strategy_version_id?: string | null;
  iterations?: RunIteration[];
  escalation?: unknown;
}

export interface AtdpEvent {
  id?: number;
  eventId?: string | null;
  runId?: string | null;
  iteration: number;
  step: number;
  node?: string | null;
  reward?: number | null;
  receivedAt?: string | null;
  payload?: string | null;
}

interface EventPayload {
  action?: {
    tool?: string;
    arguments?: unknown;
    goal?: string;
    actions?: { tool?: string }[];
    sender?: string;
    recipient?: string;
    kind?: string;
    board_plan?: {
      topology?: string;
      components?: unknown[];
    };
    assignments?: { assignee?: string }[];
  };
  outcome?: unknown;
  [key: string]: unknown;
}

export function parseRunRecord(resultJson?: string | null): RunRecord | null {
  if (!resultJson) {
    return null;
  }

  try {
    return JSON.parse(resultJson) as RunRecord;
  } catch {
    return null;
  }
}

export function parseEventPayload(payload?: string | null): EventPayload | null {
  if (!payload) {
    return null;
  }

  try {
    return JSON.parse(payload) as EventPayload;
  } catch {
    return null;
  }
}

export function formatScoreDelta(delta?: number | null): string {
  if (delta === null || delta === undefined) {
    return "-";
  }

  return delta > 0 ? `+${delta}` : String(delta);
}

export function summarizeEvent(event: AtdpEvent): string {
  const payload = parseEventPayload(event.payload);

  if (event.node === "mcp_tool" || event.node?.endsWith(".tool")) {
    const tool = payload?.action?.tool ?? "mcp_tool";
    const args = JSON.stringify(payload?.action?.arguments ?? {});
    return `${tool} ${args}`.slice(0, 140);
  }

  if (event.node?.endsWith(".plan")) {
    if (payload?.action?.board_plan) {
      const boardPlan = payload.action.board_plan;
      return `${boardPlan.topology ?? "BoardPlan"}: ${boardPlan.components?.length ?? 0} components`;
    }
    if (payload?.action?.assignments) {
      const assignees = payload.action.assignments
        .map((assignment) => assignment.assignee)
        .filter(Boolean)
        .join(", ");
      return `assign ${payload.action.assignments.length} task(s)${assignees ? `: ${assignees}` : ""}`;
    }
    const goal = payload?.action?.goal ?? "validated plan";
    const tools = (payload?.action?.actions ?? [])
      .map((action) => action.tool)
      .filter(Boolean)
      .join(" -> ");
    return `${goal}${tools ? `: ${tools}` : ""}`.slice(0, 140);
  }

  if (event.node === "blackboard.message") {
    const action = payload?.action;
    return `${action?.sender ?? "agent"} -> ${action?.recipient ?? "crew"}: ${action?.kind ?? "message"}`;
  }

  return JSON.stringify(payload?.outcome ?? payload ?? {}).slice(0, 140);
}

export function statusClassName(status?: string | null): string {
  switch (status) {
    case "converged":
    case "suggested":
      return "border-emerald-300/25 bg-emerald-300/10 text-emerald-100";
    case "running":
    case "dispatched":
    case "planning":
    case "queued":
    case "plan_approved":
      return "border-primary/25 bg-primary/10 text-primary";
    case "awaiting_plan_approval":
      return "border-amber-300/25 bg-amber-300/10 text-amber-100";
    case "failed":
    case "escalated":
    case "plan_rejected":
      return "border-red-300/25 bg-red-300/10 text-red-100";
    default:
      return "border-white/15 bg-white/5 text-gray-300";
  }
}

export function shortId(id?: string | null): string {
  return id ? id.slice(0, 8) : "-";
}

export function formatDate(value?: string | null): string {
  if (!value) {
    return "-";
  }

  return value.replace("T", " ").slice(0, 16);
}
