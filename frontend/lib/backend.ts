const DEFAULT_CONTROL_PLANE_URL = "http://control-plane:8080";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SAFE_ID_PATTERN = /^[A-Za-z0-9._:-]{1,200}$/;
const BEARER_TOKEN_PATTERN = /^[A-Za-z0-9\-._~+/]+=*$/;

export interface OidcProxyAccount {
  displayName: string;
  username: string;
  email: string | null;
}

function controlPlaneUrl(path: string): URL {
  const configured = process.env.CONTROL_PLANE_URL ?? DEFAULT_CONTROL_PLANE_URL;
  const base = new URL(configured.endsWith("/") ? configured : `${configured}/`);
  if (base.protocol !== "http:" && base.protocol !== "https:") {
    throw new Error("CONTROL_PLANE_URL must use http or https");
  }
  return new URL(path.replace(/^\//, ""), base);
}

function bearer(value: string | null | undefined, requirePrefix: boolean): string | null {
  if (!value || value.length > 16_384 || /[\u0000-\u001f\u007f]/.test(value)) return null;
  const trimmed = value.trim();
  const match = /^Bearer\s+(.+)$/i.exec(trimmed);
  if (requirePrefix && !match) return null;
  const token = (match?.[1] ?? trimmed).trim();
  return BEARER_TOKEN_PATTERN.test(token) ? `Bearer ${token}` : null;
}

export function controlPlaneAuthorization(request: Request): string | null {
  const incoming = request.headers.get("authorization");
  if (incoming !== null) return bearer(incoming, true);

  if (process.env.WEB_TRUST_OIDC_PROXY_ACCESS_TOKEN?.toLowerCase() === "true") {
    const proxyToken =
      request.headers.get("x-auth-request-access-token") ??
      request.headers.get("x-forwarded-access-token");
    if (proxyToken !== null) return bearer(proxyToken, false);
  }

  return bearer(process.env.CONTROL_PLANE_ACCESS_TOKEN, false);
}

function identityHeader(request: Request, names: string[]): string | null {
  for (const name of names) {
    const value = request.headers.get(name)?.trim();
    if (value && value.length <= 320 && !/[\u0000-\u001f\u007f]/.test(value)) return value;
  }
  return null;
}

export function oidcProxyAccount(request: Request): OidcProxyAccount | null {
  if (process.env.WEB_TRUST_OIDC_PROXY_ACCESS_TOKEN?.toLowerCase() !== "true") return null;
  const proxyToken =
    request.headers.get("x-auth-request-access-token") ??
    request.headers.get("x-forwarded-access-token");
  if (!bearer(proxyToken, false)) return null;

  const email = identityHeader(request, ["x-auth-request-email", "x-forwarded-email"]);
  const username = identityHeader(request, [
    "x-auth-request-preferred-username",
    "x-forwarded-preferred-username",
    "x-auth-request-user",
    "x-forwarded-user",
  ]) ?? email;
  if (!username) return null;

  return {
    displayName: username,
    username,
    email,
  };
}

function traceId(request: Request): string {
  const supplied = request.headers.get("x-request-id");
  return supplied && /^[A-Za-z0-9._:-]{1,128}$/.test(supplied)
    ? supplied
    : crypto.randomUUID();
}

export function problemResponse(
  request: Request,
  code: string,
  status: number,
  detail: string,
  title = "Request failed",
  extra: Record<string, unknown> = {},
): Response {
  const headers: Record<string, string> = {
    "Content-Type": "application/problem+json",
    "Cache-Control": "no-store",
  };
  if (status === 401) headers["WWW-Authenticate"] = "Bearer";
  return Response.json(
    {
      type: "about:blank",
      title,
      status,
      detail,
      instance: new URL(request.url).pathname,
      code,
      traceId: traceId(request),
      ...extra,
    },
    {
      status,
      headers,
    },
  );
}

export function jsonError(request: Request, message: string, status = 400): Response {
  return problemResponse(request, "INVALID_REQUEST", status, message, "Invalid request");
}

export function controlPlaneFetch(
  request: Request,
  path: string,
  init: RequestInit = {},
  organizationId?: string,
): Promise<Response> {
  const authorization = controlPlaneAuthorization(request);
  if (!authorization) {
    return Promise.resolve(problemResponse(
      request,
      "AUTHENTICATION_REQUIRED",
      401,
      "A valid OIDC access token is required.",
      "Authentication required",
    ));
  }

  const headers = new Headers(init.headers);
  headers.set("Authorization", authorization);
  headers.set("Accept", headers.get("Accept") ?? "application/json");
  if (organizationId) headers.set("X-Organization-ID", organizationId);
  const requestId = request.headers.get("x-request-id");
  if (requestId && /^[A-Za-z0-9._:-]{1,128}$/.test(requestId)) {
    headers.set("X-Request-ID", requestId);
  }

  return fetch(controlPlaneUrl(path), {
    ...init,
    headers,
    cache: "no-store",
  });
}

export async function readObject(request: Request): Promise<Record<string, unknown> | null> {
  const maximumBytes = 2_000_000;
  const contentLength = Number(request.headers.get("content-length") ?? "0");
  if (!Number.isFinite(contentLength) || contentLength > maximumBytes) return null;
  try {
    const body = await request.text();
    if (new TextEncoder().encode(body).byteLength > maximumBytes) return null;
    const value: unknown = JSON.parse(body);
    return value !== null && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export async function forwardJson(upstream: Response): Promise<Response> {
  const headers = new Headers({
    "Content-Type": upstream.headers.get("content-type") ?? "application/json",
    "Cache-Control": "no-store",
  });
  for (const name of ["x-request-id", "www-authenticate", "location"]) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new Response(await upstream.arrayBuffer(), { status: upstream.status, headers });
}

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

export function isSafeId(value: unknown): value is string {
  return typeof value === "string" && SAFE_ID_PATTERN.test(value);
}

export function runSubmission(
  baseRunId: string | null,
  projectId: string,
  feedback: string,
  initialRequest: Record<string, unknown>,
): { path: string; body: Record<string, unknown> } {
  return baseRunId === null
    ? { path: `/api/v1/projects/${encodeURIComponent(projectId)}/runs`, body: initialRequest }
    : {
        path: `/api/v1/runs/${encodeURIComponent(baseRunId)}/revisions`,
        body: { feedback },
      };
}
