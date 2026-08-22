import {
  controlPlaneFetch,
  forwardJson,
  isSafeId,
  isUuid,
  jsonError,
  problemResponse,
  readObject,
} from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function POST(request: Request): Promise<Response> {
  const input = await readObject(request);
  if (!input) return jsonError(request, "A JSON request body is required.");
  if (!isUuid(input.organization_id) || !isUuid(input.project_id)) {
    return jsonError(request, "organization_id and project_id must be UUIDs.");
  }
  if (!isSafeId(input.thread_id)) return jsonError(request, "thread_id is invalid.");

  try {
    const upstream = await controlPlaneFetch(
      request,
      `/api/v1/projects/${encodeURIComponent(input.project_id)}/threads/${encodeURIComponent(input.thread_id)}/messages`,
      { signal: request.signal },
      input.organization_id,
    );
    return forwardJson(upstream);
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "Conversation history is unavailable.",
    );
  }
}
