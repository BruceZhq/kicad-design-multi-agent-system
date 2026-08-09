import {
  controlPlaneFetch,
  forwardJson,
  jsonError,
  problemResponse,
  readObject,
} from "@/lib/backend";
import { parseProfileUpdate } from "@/types/profile";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request): Promise<Response> {
  try {
    return forwardJson(await controlPlaneFetch(
      request,
      "/api/v1/me/profile",
      { method: "GET", signal: request.signal },
    ));
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The user profile service is unavailable.",
    );
  }
}

export async function PUT(request: Request): Promise<Response> {
  const body = parseProfileUpdate(await readObject(request));
  if (!body) {
    return jsonError(request, "The profile body or optimistic version is invalid.");
  }
  try {
    return forwardJson(await controlPlaneFetch(
      request,
      "/api/v1/me/profile",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: request.signal,
      },
    ));
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The user profile service is unavailable.",
    );
  }
}
