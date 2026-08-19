import {
  controlPlaneFetch,
  forwardJson,
  isUuid,
  jsonError,
  problemResponse,
} from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request): Promise<Response> {
  const query = new URL(request.url).searchParams;
  const organizationId = query.get("organization_id");
  const projectId = query.get("project_id");
  if (!isUuid(organizationId) || !isUuid(projectId)) {
    return jsonError(request, "organization_id and project_id must be UUIDs.");
  }
  try {
    const upstream = await controlPlaneFetch(
      request,
      `/api/v1/projects/${encodeURIComponent(projectId)}/threads`,
      { signal: request.signal },
      organizationId,
    );
    return forwardJson(upstream);
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "Conversation list is unavailable.",
    );
  }
}
