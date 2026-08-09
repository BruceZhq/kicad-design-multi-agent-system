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
  params: Promise<{ artifactId: string }>;
}

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  const { artifactId } = await context.params;
  const organizationId = new URL(request.url).searchParams.get("organization_id");
  if (!isUuid(artifactId)) return jsonError(request, "artifactId must be a UUID.");
  if (!isUuid(organizationId)) return jsonError(request, "organization_id must be a UUID.");
  try {
    const upstream = await controlPlaneFetch(
      request,
      `/api/v1/artifacts/${encodeURIComponent(artifactId)}:download`,
      { method: "GET", redirect: "manual", signal: request.signal },
      organizationId,
    );
    return forwardJson(upstream);
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The artifact download service is unavailable.",
    );
  }
}
