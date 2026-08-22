import { oidcProxyAccount, problemResponse } from "@/lib/backend";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export async function GET(request: Request): Promise<Response> {
  const account = oidcProxyAccount(request);
  if (!account) {
    return problemResponse(
      request,
      "AUTHENTICATION_REQUIRED",
      401,
      "A valid OAuth2 Proxy session is required.",
      "Authentication required",
    );
  }
  return Response.json(account, { headers: { "Cache-Control": "no-store" } });
}
