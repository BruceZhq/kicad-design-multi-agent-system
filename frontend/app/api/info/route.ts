import {
  controlPlaneFetch,
  forwardJson,
  isUuid,
  jsonError,
  problemResponse,
} from "@/lib/backend";
import { parseCapabilityProfile } from "@/types/chat";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const organizationId = url.searchParams.get("organization_id");
  const projectId = url.searchParams.get("project_id");
  if (!isUuid(organizationId) || !isUuid(projectId)) {
    return jsonError(request, "organization_id and project_id must be UUIDs.");
  }
  try {
    const upstream = await controlPlaneFetch(
      request,
      `/api/v1/projects/${encodeURIComponent(projectId)}/runtime-info`,
      { signal: request.signal },
      organizationId,
    );
    if (!upstream.ok) return forwardJson(upstream);
    const value = (await upstream.json().catch(() => null)) as Record<string, unknown> | null;
    const agents = value?.agents;
    const models = value?.models;
    const defaultAgent = value?.defaultAgent;
    const defaultModel = value?.defaultModel;
    const profiles = value?.profiles;
    const validAgents = Array.isArray(agents) && agents.every((agent) => {
      if (!agent || typeof agent !== "object") return false;
      const item = agent as Record<string, unknown>;
      return typeof item.key === "string" && typeof item.description === "string";
    });
    const validModels = Array.isArray(models) && models.every((model) => typeof model === "string");
    const parsedProfiles = Array.isArray(profiles)
      ? profiles.map(parseCapabilityProfile)
      : [];
    const validProfiles =
      parsedProfiles.length > 0 &&
      parsedProfiles.every((profile) => profile !== null) &&
      new Set(parsedProfiles.map((profile) => `${profile?.id}@${profile?.version}`)).size ===
        parsedProfiles.length;
    if (
      !value ||
      !validAgents ||
      !validModels ||
      !validProfiles ||
      typeof defaultAgent !== "string" ||
      typeof defaultModel !== "string" ||
      !(agents as Array<Record<string, unknown>>).some((agent) => agent.key === defaultAgent) ||
      !(models as string[]).includes(defaultModel)
    ) {
      return problemResponse(
        request,
        "CONTROL_PLANE_INVALID_RESPONSE",
        502,
        "The control plane returned invalid runtime metadata.",
      );
    }
    return Response.json(
      {
        agents,
        models,
        default_agent: defaultAgent,
        default_model: defaultModel,
        capability_profiles: parsedProfiles,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "Runtime metadata is unavailable.",
    );
  }
}
