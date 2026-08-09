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

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  const { requestId: runId } = await context.params;
  const organizationId = new URL(request.url).searchParams.get("organization_id");
  if (!isUuid(runId)) return jsonError(request, "runId must be a UUID.");
  if (!isUuid(organizationId)) return jsonError(request, "organization_id must be a UUID.");
  try {
    return forwardJson(await controlPlaneFetch(
      request,
      `/api/v1/runs/${encodeURIComponent(runId)}/artifacts`,
      { method: "GET", signal: request.signal },
      organizationId,
    ));
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The artifact manifest service is unavailable.",
    );
  }
}
