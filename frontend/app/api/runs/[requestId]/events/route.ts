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

  const headers = new Headers({ Accept: "text/event-stream" });
  const lastEventId = request.headers.get("last-event-id");
  if (lastEventId && /^\d+$/.test(lastEventId)) headers.set("Last-Event-ID", lastEventId);
  try {
    const upstream = await controlPlaneFetch(
      request,
      `/api/v1/runs/${encodeURIComponent(runId)}/events`,
      { headers, signal: request.signal },
      organizationId,
    );
    if (!upstream.ok || !upstream.body) return forwardJson(upstream);
    const responseHeaders = new Headers({
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no",
      "X-Run-ID": runId,
    });
    const requestId = upstream.headers.get("x-request-id");
    if (requestId) responseHeaders.set("X-Request-ID", requestId);
    return new Response(upstream.body, { status: 200, headers: responseHeaders });
  } catch {
    if (request.signal.aborted) return new Response(null, { status: 499 });
    return problemResponse(request, "CONTROL_PLANE_UNAVAILABLE", 502, "The run event stream is unavailable.");
  }
}
