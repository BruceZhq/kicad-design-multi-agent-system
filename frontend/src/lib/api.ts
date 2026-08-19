import type {
  AtdpEvent,
  AuthResult,
  CreateRunResponse,
  DesignBackend,
  DesignRun,
  HealthResponse,
  TenantContext,
  RunApproval,
  BoardPlan,
  DesignPlan
} from "./runData";

function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return extra;
}

async function requestJson<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = authHeaders(options.headers as Record<string, string>);
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers["X-RatsNest-Client"] = "web";
  }
  const response = await fetch(path, {
    ...options,
    credentials: "same-origin",
    headers
  });

  if (!response.ok) {
    const body = await response.text();
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      detail = parsed.detail || parsed.error || body;
    } catch {
      // keep raw body
    }
    const error = new Error(detail || `${response.status} ${response.statusText}`);
    (error as Error & { status?: number }).status = response.status;
    throw error;
  }

  return (await response.json()) as T;
}

export function getHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/api/health");
}

export function listRuns(): Promise<DesignRun[]> {
  return requestJson<DesignRun[]>("/api/runs");
}

export function getRun(id: string): Promise<DesignRun> {
  return requestJson<DesignRun>(`/api/runs/${encodeURIComponent(id)}`);
}

export function getRunEvents(id: string): Promise<AtdpEvent[]> {
  return requestJson<AtdpEvent[]>(
    `/api/runs/${encodeURIComponent(id)}/events`
  );
}

export function createDesignRun(
  requirement: string,
  backend: DesignBackend,
  projectId?: string | null
): Promise<CreateRunResponse> {
  return requestJson<CreateRunResponse>("/api/designs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID()
    },
    body: JSON.stringify({ requirement, backend, projectId })
  });
}

export function createRepairRun(
  projectDir: string,
  projectId?: string | null
): Promise<CreateRunResponse> {
  return requestJson<CreateRunResponse>("/api/runs", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID()
    },
    body: JSON.stringify({ projectDir, projectId })
  });
}

export function getTenantContext(): Promise<TenantContext> {
  return requestJson<TenantContext>("/api/tenant/context");
}

// -- auth ---------------------------------------------------------------------

export async function register(
  username: string,
  password: string
): Promise<AuthResult> {
  return requestJson<AuthResult>("/api/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password })
  });
}

export async function login(
  username: string,
  password: string
): Promise<AuthResult> {
  const result = await requestJson<AuthResult>("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password })
  });
  return result;
}

export async function logout(): Promise<void> {
  try {
    await fetch("/api/auth/logout", {
      method: "POST",
      credentials: "same-origin",
      headers: authHeaders({ "X-RatsNest-Client": "web" })
    });
  } catch {
    // best effort
  }
}

export async function getMe(): Promise<AuthResult> {
  return requestJson<AuthResult>("/api/auth/me");
}

// -- artifact URLs (cookie carries auth in jwt mode; direct in open mode) ------

export function downloadUrl(id: string): string {
  return `/api/runs/${encodeURIComponent(id)}/download`;
}

export function previewUrl(id: string, which: string): string {
  return `/api/runs/${encodeURIComponent(id)}/preview/${encodeURIComponent(which)}`;
}

export function listSteps(id: string): Promise<string[]> {
  return requestJson<string[]>(`/api/runs/${encodeURIComponent(id)}/steps`);
}

export interface EdaPin {
  pin: string;
  net: string | null;
}

export interface EdaComponent {
  ref: string;
  value: string;
  lib_id: string;
  x: number;
  y: number;
  pins: EdaPin[];
}

export interface EdaState {
  schematic: string;
  components: EdaComponent[];
  nets: string[];
  palette: string[];
  sheet: { width: number; height: number };
  applied?: string[];
  errors?: string[];
}

export type EdaOp =
  | { op: "move"; ref: string; x: number; y: number }
  | { op: "set_value"; ref: string; value: string }
  | { op: "set_property"; ref: string; name: string; value: string }
  | { op: "add_component"; ref: string; symbol: string; value: string;
      x: number; y: number }
  | { op: "connect_net"; ref: string; pin: string; net: string };

export function getEdaState(id: string): Promise<EdaState> {
  return requestJson<EdaState>(`/api/runs/${encodeURIComponent(id)}/eda`);
}

export function applyEdaOps(id: string, ops: EdaOp[]): Promise<EdaState> {
  return requestJson<EdaState>(`/api/runs/${encodeURIComponent(id)}/eda`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(ops)
  });
}

export async function getReport(id: string): Promise<string | null> {
  const response = await fetch(`/api/runs/${encodeURIComponent(id)}/report`, {
    credentials: "same-origin",
    headers: authHeaders()
  });
  return response.ok ? response.text() : null;
}

export async function getRunApproval(id: string): Promise<RunApproval | null> {
  const response = await fetch(
    `/api/runs/${encodeURIComponent(id)}/approval`,
    { credentials: "same-origin", headers: authHeaders() }
  );
  if (response.status === 204 || response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as RunApproval;
}

export function getRunApprovals(id: string): Promise<RunApproval[]> {
  return requestJson<RunApproval[]>(
    `/api/runs/${encodeURIComponent(id)}/approvals`
  );
}

export async function getDesignPlan(id: string): Promise<DesignPlan | null> {
  const response = await fetch(
    `/api/runs/${encodeURIComponent(id)}/plan`,
    { credentials: "same-origin", headers: authHeaders() }
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as DesignPlan;
}

export async function getBoardPlan(id: string): Promise<BoardPlan | null> {
  const response = await fetch(
    `/api/runs/${encodeURIComponent(id)}/board-plan`,
    { credentials: "same-origin", headers: authHeaders() }
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return (await response.json()) as BoardPlan;
}

export function decideRunApproval(
  id: string,
  type: "board_plan" | "design_release",
  decision: "approved" | "rejected",
  comment: string
): Promise<RunApproval> {
  return requestJson<RunApproval>(
    `/api/runs/${encodeURIComponent(id)}/approvals/${encodeURIComponent(type)}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, comment })
    }
  );
}
