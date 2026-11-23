import { NextResponse, type NextRequest } from "next/server";
import { authConfig, AuthMisconfigured, readSession, sessionCookie } from "@/lib/auth";

// With sign-in configured, nothing but /auth/* and static assets is served without a session.
// The API proxy route checks the session again itself (lib/auth is the single source).
export async function proxy(req: NextRequest) {
  let config;
  try {
    config = authConfig();
  } catch (error) {
    if (!(error instanceof AuthMisconfigured)) throw error;
    return new Response(`Sign-in is not set up: ${error.message}`, { status: 500, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" } });
  }
  if (!config) return NextResponse.next();
  if (await readSession(config, req.cookies.get(sessionCookie(config))?.value)) return NextResponse.next();
  if (req.nextUrl.pathname.startsWith("/api/")) {
    return Response.json({ code: "unauthenticated", message: "Sign in to use the Explorer." }, { status: 401, headers: { "Cache-Control": "no-store" } });
  }
  const login = new URL("/auth/login", config.publicUrl);
  login.searchParams.set("return_to", req.nextUrl.pathname + req.nextUrl.search);
  return NextResponse.redirect(login, 303);
}

export const config = {
  matcher: ["/((?!auth/|_next/static|_next/image|favicon.ico).*)"],
};
