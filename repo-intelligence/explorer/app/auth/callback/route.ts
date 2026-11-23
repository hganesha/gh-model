import { timingSafeEqual } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";
import { authConfig, AuthMisconfigured, completeLogin, cookieOptions, readState, SESSION_MAX_AGE, sessionCookie, signSession, stateCookie } from "@/lib/auth";
import { authPage } from "@/lib/auth-page";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const same = (a: string, b: string) => a.length === b.length && timingSafeEqual(Buffer.from(a), Buffer.from(b));
const retry = { href: "/auth/login", label: "Sign in again" };

export async function GET(req: NextRequest) {
  let config;
  try {
    config = authConfig();
  } catch (error) {
    if (error instanceof AuthMisconfigured) return authPage(500, "Sign-in is not set up", error.message);
    throw error;
  }
  if (!config) return NextResponse.redirect(new URL("/", req.url), 303);
  const params = req.nextUrl.searchParams;
  // The provider's own error text is not shown: it is attacker-controllable through this URL.
  if (params.get("error")) return authPage(401, "Sign-in was not completed", "The identity provider did not sign you in. If you were not expecting this, ask your administrator to assign you to the RKP Explorer application.", retry);
  const login = await readState(config, req.cookies.get(stateCookie(config))?.value);
  const code = params.get("code"), state = params.get("state");
  if (!login || !code || !state || !same(state, login.state)) {
    return authPage(400, "Sign-in expired", "This sign-in link is no longer valid. Start again.", retry);
  }
  let user;
  try {
    user = await completeLogin(config, code, login);
  } catch {
    return authPage(401, "Sign-in failed", "The identity provider's answer could not be verified, so you were not signed in.", retry);
  }
  const res = NextResponse.redirect(new URL(login.returnTo, config.publicUrl), 303);
  res.cookies.set(stateCookie(config), "", cookieOptions(config, 0));
  res.cookies.set(sessionCookie(config), await signSession(config, user), cookieOptions(config, SESSION_MAX_AGE));
  res.headers.set("Cache-Control", "no-store");
  return res;
}
