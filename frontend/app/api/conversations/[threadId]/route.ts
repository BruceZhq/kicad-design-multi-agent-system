import {
  controlPlaneFetch,
  forwardJson,
  isSafeId,
  isUuid,
  jsonError,
  problemResponse,
} from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

interface RouteContext {
  params: Promise<{ threadId: string }>;
}

export async function DELETE(request: Request, context: RouteContext): Promise<Response> {
  const { threadId } = await context.params;
  const query = new URL(request.url).searchParams;
  const organizationId = query.get("organization_id");
  const projectId = query.get("project_id");
  if (!isSafeId(threadId)) return jsonError(request, "thread_id is invalid.");
  if (!isUuid(organizationId) || !isUuid(projectId)) {
    return jsonError(request, "organization_id and project_id must be UUIDs.");
  }
  try {
    const upstream = await controlPlaneFetch(
      request,
      `/api/v1/projects/${encodeURIComponent(projectId)}/threads/${encodeURIComponent(threadId)}`,
      { method: "DELETE", signal: request.signal },
      organizationId,
    );
    if (upstream.status === 204) {
      return new Response(null, {
        status: 204,
        headers: { "Cache-Control": "no-store" },
      });
    }
    return forwardJson(upstream);
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "Conversation deletion is unavailable.",
    );
  }
}
