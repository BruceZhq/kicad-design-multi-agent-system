import {
  controlPlaneFetch,
  forwardJson,
  isUuid,
  jsonError,
  problemResponse,
  readObject,
} from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface RouteContext {
  params: Promise<{ requestId: string; interactionId: string }>;
}

export async function POST(request: Request, context: RouteContext): Promise<Response> {
  const { requestId: runId, interactionId } = await context.params;
  const input = await readObject(request);
  if (!isUuid(runId)) return jsonError(request, "runId must be a UUID.");
  if (!/^[A-Za-z0-9._:-]{1,200}$/.test(interactionId)) {
    return jsonError(request, "interactionId contains unsupported characters.");
  }
  if (!input) return jsonError(request, "A JSON request body is required.");

  const organizationId = input.organization_id;
  const idempotencyKey = input.request_id;
  const answer = typeof input.answer === "string" ? input.answer.trim() : "";
  const stateVersion = input.state_version;
  if (!isUuid(organizationId)) return jsonError(request, "organization_id must be a UUID.");
  if (typeof idempotencyKey !== "string" || !/^[A-Za-z0-9._:-]{8,200}$/.test(idempotencyKey)) {
    return jsonError(request, "request_id must contain between 8 and 200 safe characters.");
  }
  if (!answer || answer.length > 100_000) {
    return jsonError(request, "answer must contain between 1 and 100000 characters.");
  }
  if (!Number.isSafeInteger(stateVersion) || Number(stateVersion) < 1) {
    return jsonError(request, "state_version must be a positive integer.");
  }

  try {
    return forwardJson(await controlPlaneFetch(
      request,
      `/api/v1/runs/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}:respond`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey,
        },
        body: JSON.stringify({ answer, stateVersion }),
        signal: request.signal,
      },
      organizationId,
    ));
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The human-input response service is unavailable.",
    );
  }
}
