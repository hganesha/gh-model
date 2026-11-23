import { NextResponse, type NextRequest } from "next/server";
import { authConfig, cookieOptions, discover, secureUrl, sessionCookie } from "@/lib/auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** POST only, from this origin: a link elsewhere cannot sign someone out. Ends the provider session too. */
export async function POST(req: NextRequest) {
  const config = authConfig();
  if (!config) return NextResponse.redirect(new URL("/", req.url), 303);
  // Browsers say where a request came from in Sec-Fetch-Site; Origin can be "null" under a no-referrer policy.
  const site = req.headers.get("sec-fetch-site");
  const origin = req.headers.get("origin");
  const sameSite = site ? site === "same-origin" || site === "none" : !origin || origin === config.publicUrl;
  if (!sameSite) return new Response("cross-origin sign-out refused", { status: 403 });
  let target = new URL("/auth/signed-out", config.publicUrl);
  try {
    const end = (await discover(config)).end_session_endpoint;
    if (secureUrl(end)) {
      target = new URL(end);
      target.searchParams.set("post_logout_redirect_uri", `${config.publicUrl}/auth/signed-out`);
      target.searchParams.set("client_id", config.clientId);
    }
  } catch {
    // The local session still ends; the provider session times out on its own.
  }
  const res = NextResponse.redirect(target, 303);
  res.cookies.set(sessionCookie(config), "", cookieOptions(config, 0));
  res.headers.set("Cache-Control", "no-store");
  return res;
}
