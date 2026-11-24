import { NextResponse, type NextRequest } from "next/server";
import { authConfig, AuthMisconfigured, cookieOptions, discover, pkceChallenge, randomToken, safeReturnPath, signState, STATE_MAX_AGE, stateCookie } from "@/lib/auth";
import { authPage } from "@/lib/auth-page";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  let config;
  try {
    config = authConfig();
  } catch (error) {
    if (error instanceof AuthMisconfigured) return authPage(500, "Sign-in is not set up", error.message);
    throw error;
  }
  if (!config) return NextResponse.redirect(new URL("/", req.url), 303);
  let endpoint: string;
  try {
    endpoint = (await discover(config)).authorization_endpoint;
  } catch {
    return authPage(502, "Sign-in is unavailable", "The identity provider could not be reached. Try again in a moment.", { href: "/auth/login", label: "Try again" });
  }
  const login = { state: randomToken(), nonce: randomToken(), verifier: randomToken() + randomToken(), returnTo: safeReturnPath(req.nextUrl.searchParams.get("return_to")) };
  const url = new URL(endpoint);
  url.search = new URLSearchParams({
    response_type: "code",
    client_id: config.clientId,
    redirect_uri: `${config.publicUrl}/auth/callback`,
    scope: "openid profile email",
    response_mode: "query",
    state: login.state,
    nonce: login.nonce,
    code_challenge: await pkceChallenge(login.verifier),
    code_challenge_method: "S256",
  }).toString();
  const res = NextResponse.redirect(url, 303);
  res.cookies.set(stateCookie(config), await signState(config, login), cookieOptions(config, STATE_MAX_AGE));
  res.headers.set("Cache-Control", "no-store");
  return res;
}
