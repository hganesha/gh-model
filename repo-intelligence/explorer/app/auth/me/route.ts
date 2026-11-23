import type { NextRequest } from "next/server";
import { authConfig, readSession, sessionCookie } from "@/lib/auth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Who is signed in, for the top bar. `enabled: false` when the Explorer runs without sign-in. */
export async function GET(req: NextRequest) {
  const headers = { "Cache-Control": "no-store" };
  const config = authConfig();
  if (!config) return Response.json({ enabled: false, user: null }, { headers });
  const user = await readSession(config, req.cookies.get(sessionCookie(config))?.value);
  if (!user) return Response.json({ enabled: true, user: null }, { status: 401, headers });
  return Response.json({ enabled: true, user: { name: user.name, login: user.login, role: user.role } }, { headers });
}
