import {
  controlPlaneFetch,
  forwardJson,
  isUuid,
  jsonError,
  problemResponse,
} from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface RouteContext {
  params: Promise<{ requestId: string }>;
}

export async function POST(request: Request, context: RouteContext): Promise<Response> {
  const { requestId: runId } = await context.params;
  const organizationId = new URL(request.url).searchParams.get("organization_id");
  const idempotencyKey = request.headers.get("idempotency-key");
  if (!isUuid(runId)) return jsonError(request, "runId must be a UUID.");
  if (!isUuid(organizationId)) return jsonError(request, "organization_id must be a UUID.");
  if (!idempotencyKey || !/^[A-Za-z0-9._:-]{8,200}$/.test(idempotencyKey)) {
    return jsonError(request, "Idempotency-Key must contain between 8 and 200 safe characters.");
  }
  try {
    return forwardJson(await controlPlaneFetch(
      request,
      `/api/v1/runs/${encodeURIComponent(runId)}:recover`,
      { method: "POST", headers: { "Idempotency-Key": idempotencyKey }, signal: request.signal },
      organizationId,
    ));
  } catch {
    return problemResponse(request, "CONTROL_PLANE_UNAVAILABLE", 502, "The run recovery service is unavailable.");
  }
}
