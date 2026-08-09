export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function configuredAccountCenter(): URL | null {
  const configured = process.env.OIDC_ACCOUNT_URL?.trim();
  if (!configured) return null;
  try {
    const url = new URL(configured);
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

export function GET(): Response {
  const accountCenter = configuredAccountCenter();
  if (!accountCenter) {
    return Response.json(
      {
        type: "about:blank",
        title: "Enterprise account center unavailable",
        status: 503,
        detail: "The OIDC account center URL is not configured.",
      },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
  return Response.redirect(accountCenter, 302);
}
