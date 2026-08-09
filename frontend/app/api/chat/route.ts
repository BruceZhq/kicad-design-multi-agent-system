import {
  controlPlaneFetch,
  forwardJson,
  isSafeId,
  isUuid,
  jsonError,
  problemResponse,
  readObject,
  runSubmission,
} from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface TeamMember {
  roleId: string;
  name: string;
  responsibility: string;
}

interface StartedRun {
  runId: string;
  state: string;
  createdAt?: string;
  errorCode?: string | null;
  error?: string | null;
}

interface CapabilityProfileSelector {
  id: string;
  version: string;
}

function capabilityProfile(value: unknown): CapabilityProfileSelector | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const item = value as Record<string, unknown>;
  if (
    typeof item.id !== "string" ||
    !/^[a-z0-9][a-z0-9-]{1,63}$/.test(item.id) ||
    typeof item.version !== "string" ||
    item.version.length > 32 ||
    !/^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))?$/.test(item.version)
  ) return null;
  return { id: item.id, version: item.version };
}

function teamMembers(value: unknown): TeamMember[] | null {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 8) return null;
  const result: TeamMember[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") return null;
    const member = item as Record<string, unknown>;
    const roleId = member.role_id ?? member.roleId;
    if (
      typeof roleId !== "string" ||
      !/^[a-z0-9][a-z0-9-]{1,63}$/.test(roleId) ||
      typeof member.name !== "string" ||
      member.name.length < 1 ||
      member.name.length > 80 ||
      typeof member.responsibility !== "string" ||
      member.responsibility.length < 1 ||
      member.responsibility.length > 500
    ) return null;
    result.push({
      roleId,
      name: member.name,
      responsibility: member.responsibility,
    });
  }
  return result;
}

function eventCursor(value: unknown): number | null {
  return Number.isSafeInteger(value) && Number(value) >= 0 ? Number(value) : null;
}

export async function POST(request: Request): Promise<Response> {
  const input = await readObject(request);
  if (!input) return jsonError(request, "A JSON request body is required.");

  const message = typeof input.message === "string" ? input.message.trim() : "";
  const organizationId = input.organization_id;
  const projectId = input.project_id;
  const baseRunId = input.base_run_id;
  const threadId = input.thread_id;
  const idempotencyKey = input.request_id;
  const lastEventId = eventCursor(input.last_event_id ?? 0);
  const model = input.model;
  const selectedProfile = capabilityProfile(input.capability_profile);
  if (!message || message.length > 100_000) {
    return jsonError(request, "message must contain between 1 and 100000 characters.");
  }
  if (!isUuid(organizationId) || !isUuid(projectId)) {
    return jsonError(request, "organization_id and project_id must be UUIDs.");
  }
  if (baseRunId !== undefined && baseRunId !== null && !isUuid(baseRunId)) {
    return jsonError(request, "base_run_id must be a UUID when provided.");
  }
  if (!isSafeId(threadId)) return jsonError(request, "thread_id is invalid.");
  if (typeof idempotencyKey !== "string" || !/^[A-Za-z0-9._:-]{8,200}$/.test(idempotencyKey)) {
    return jsonError(request, "request_id must contain between 8 and 200 safe characters.");
  }
  if (lastEventId === null) return jsonError(request, "last_event_id must be a non-negative integer.");
  if (model !== undefined && model !== null && (typeof model !== "string" || model.length > 200)) {
    return jsonError(request, "model must be a string of at most 200 characters.");
  }
  if (!selectedProfile) {
    return jsonError(request, "capability_profile must contain a valid id and version.");
  }

  const members = teamMembers(
    input.team_members ??
    (input.agent_config && typeof input.agent_config === "object"
      ? (input.agent_config as Record<string, unknown>).team_members
      : undefined),
  );
  if (members === null) {
    return jsonError(request, "team_members must contain at most 8 valid team members.");
  }

  try {
    const revisionRunId = typeof baseRunId === "string" ? baseRunId : null;
    const submission = runSubmission(revisionRunId, projectId, message, {
      message,
      model: typeof model === "string" ? model : null,
      threadId,
      teamMembers: members,
      capabilityProfile: selectedProfile,
    });
    const started = await controlPlaneFetch(
      request,
      submission.path,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify(submission.body),
        signal: request.signal,
      },
      organizationId,
    );
    if (!started.ok) return forwardJson(started);
    const run = (await started.json().catch(() => null)) as StartedRun | null;
    if (!run || !isUuid(run.runId) || typeof run.state !== "string") {
      return problemResponse(
        request,
        "CONTROL_PLANE_INVALID_RESPONSE",
        502,
        "The control plane returned an invalid run identifier.",
      );
    }
    if (run.state === "FAILED" && run.errorCode === "RUNTIME_START_FAILED") {
      return problemResponse(
        request,
        "RUNTIME_START_FAILED",
        502,
        run.error || "The Agent Runtime could not start the run.",
        "Agent Runtime start failed",
        { runId: run.runId },
      );
    }

    const eventHeaders = new Headers({ Accept: "text/event-stream" });
    if (lastEventId > 0) eventHeaders.set("Last-Event-ID", String(lastEventId));
    const events = await controlPlaneFetch(
      request,
      `/api/v1/runs/${encodeURIComponent(run.runId)}/events`,
      { headers: eventHeaders, signal: request.signal },
      organizationId,
    );
    if (!events.ok || !events.body) {
      const forwarded = await forwardJson(events);
      const headers = new Headers(forwarded.headers);
      headers.set("X-Run-ID", run.runId);
      return new Response(forwarded.body, { status: forwarded.status, headers });
    }

    const headers = new Headers({
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no",
      "X-Run-ID": run.runId,
    });
    const requestId = events.headers.get("x-request-id") ?? started.headers.get("x-request-id");
    if (requestId) headers.set("X-Request-ID", requestId);
    return new Response(events.body, { status: 200, headers });
  } catch {
    if (request.signal.aborted) return new Response(null, { status: 499 });
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The control plane run stream is unavailable.",
    );
  }
}
