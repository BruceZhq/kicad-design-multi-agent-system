import {
  controlPlaneFetch,
  jsonError,
  problemResponse,
} from "@/lib/backend";
import { MAX_AVATAR_BYTES, isAvatarFile } from "@/types/profile";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const MAX_MULTIPART_BYTES = MAX_AVATAR_BYTES + 128 * 1024;

async function forwardBody(upstream: Response): Promise<Response> {
  const headers = new Headers({ "Cache-Control": "no-store" });
  for (const name of ["content-type", "etag", "last-modified", "x-request-id", "www-authenticate"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new Response(await upstream.arrayBuffer(), { status: upstream.status, headers });
}

export async function GET(request: Request): Promise<Response> {
  try {
    return forwardBody(await controlPlaneFetch(
      request,
      "/api/v1/me/profile/avatar",
      { method: "GET", signal: request.signal },
    ));
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The profile avatar service is unavailable.",
    );
  }
}

export async function PUT(request: Request): Promise<Response> {
  const contentLength = Number(request.headers.get("content-length") ?? "0");
  if (!Number.isFinite(contentLength) || contentLength < 0 || contentLength > MAX_MULTIPART_BYTES) {
    return jsonError(request, "The avatar upload must not exceed 2 MiB.", 413);
  }

  let input: FormData;
  try {
    input = await request.formData();
  } catch {
    return jsonError(request, "A multipart avatar upload is required.");
  }
  const file = input.get("file");
  const rawVersion = input.get("version");
  const version = typeof rawVersion === "string" && /^\d+$/.test(rawVersion)
    ? Number(rawVersion)
    : Number.NaN;
  if (!(file instanceof File) || !isAvatarFile(file)) {
    return jsonError(request, "Avatar must be a JPEG, PNG or WebP file no larger than 2 MiB.");
  }
  if (!Number.isSafeInteger(version) || version < 0) {
    return jsonError(request, "A valid optimistic profile version is required.");
  }

  const body = new FormData();
  body.set("version", String(version));
  body.set("file", file, file.name);
  try {
    return forwardBody(await controlPlaneFetch(
      request,
      "/api/v1/me/profile/avatar",
      { method: "PUT", body, signal: request.signal },
    ));
  } catch {
    return problemResponse(
      request,
      "CONTROL_PLANE_UNAVAILABLE",
      502,
      "The profile avatar service is unavailable.",
    );
  }
}
