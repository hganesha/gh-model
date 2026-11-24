import { readFile } from "node:fs/promises";
import path from "node:path";
import type { NextRequest } from "next/server";
import { actorOf, authConfig, readSession, sessionCookie } from "@/lib/auth";
import { record, recordDir, replay, replayDir } from "@/lib/replay";

// Server-side proxy to the RKP API. The bearer token is attached here and never
// leaves the server: it is not echoed in responses, errors or headers. Headers are
// built from scratch, so a browser cannot supply its own X-RKP-Actor or X-RKP-Role.

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const API_ROOTS = new Set(["repositories", "compilations", "snapshots", "knowledge", "evidence", "business-maps", "settings"]);
const SAFE_SEGMENT = /^[A-Za-z0-9._:@~-]+$/;
const MAX_BODY_BYTES = 384 * 1024;

type Context = { params: Promise<{ path: string[] }> };

function problem(status: number, code: string, message: string): Response {
  return Response.json({ code, message }, { status, headers: { "Cache-Control": "no-store" } });
}

async function readToken(): Promise<string | null> {
  const fromEnv = process.env.RKP_DEV_TOKEN?.trim();
  if (fromEnv) return fromEnv;
  try {
    const file = path.resolve(process.cwd(), "..", ".rkp-store", "dev-token");
    return (await readFile(file, "utf8")).trim() || null;
  } catch {
    return null;
  }
}

function sameOrigin(req: NextRequest): boolean {
  const origin = req.headers.get("origin");
  if (!origin) return true; // same-origin GETs and non-browser clients send none
  try {
    return new URL(origin).host === req.headers.get("host");
  } catch {
    return false;
  }
}

async function forward(req: NextRequest, ctx: Context): Promise<Response> {
  const { path: segments } = await ctx.params;
  if (!segments?.length || !API_ROOTS.has(segments[0])) return problem(404, "not_found", "not found");
  if (segments.some((s) => s === "." || s === ".." || !SAFE_SEGMENT.test(s))) {
    return problem(400, "bad_path", "the request path contains unsupported characters");
  }
  if (!sameOrigin(req)) return problem(403, "forbidden", "cross-origin requests are not allowed");

  // With sign-in on, every call carries the signed-in user; the API records them and refuses writes from viewers.
  const auth = authConfig();
  const user = auth ? await readSession(auth, req.cookies.get(sessionCookie(auth))?.value) : null;
  if (auth && !user) return problem(401, "unauthenticated", "Sign in to use the Explorer.");

  // Slashes between segments are kept (repository ids contain one); ":" stays readable for sha256:<hex>.
  const upstreamPath = segments.map((s) => encodeURIComponent(s).replace(/%3A/gi, ":").replace(/%40/g, "@")).join("/");
  const apiPath = `/v1/${upstreamPath}${req.nextUrl.search}`;

  const replaying = replayDir();
  if (replaying) {
    const text = req.method === "POST" ? await req.text() : undefined;
    const hit = await replay(replaying, req.method, apiPath, text);
    if (!hit) return problem(404, "not_recorded", "This Explorer replays recorded responses, and this request was not recorded.");
    return new Response(hit.body, { status: hit.status, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } });
  }

  const token = await readToken();
  if (!token) {
    return problem(500, "proxy_not_configured", "The explorer has no API token. Set RKP_DEV_TOKEN or create ../.rkp-store/dev-token.");
  }

  const base = (process.env.RKP_API_URL ?? "http://127.0.0.1:8787").replace(/\/+$/, "");
  const url = `${base}${apiPath}`;

  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  if (user) {
    headers["X-RKP-Actor"] = actorOf(user);
    headers["X-RKP-Role"] = user.role;
  }
  let body: string | undefined;
  if (req.method === "POST") {
    if (!(req.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json")) {
      return problem(415, "unsupported_media_type", "POST bodies must be application/json");
    }
    body = await req.text();
    if (Buffer.byteLength(body) > MAX_BODY_BYTES) return problem(413, "too_large", "request body is too large");
    headers["Content-Type"] = "application/json";
  }

  const isEventStream = segments[segments.length - 1] === "events";
  if (isEventStream) headers.Accept = "text/event-stream";

  let upstream: Response;
  try {
    upstream = await fetch(url, { method: req.method, headers, body, cache: "no-store", redirect: "manual", signal: req.signal });
  } catch {
    return problem(502, "upstream_unreachable", "The RKP API could not be reached.");
  }

  if (isEventStream && upstream.ok && upstream.body) {
    // Pass the byte stream straight through; no-transform keeps compression from buffering it.
    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
  }

  // JSON answers are read fully and re-emitted rather than handing the upstream stream to the client.
  let text: string;
  try {
    text = await upstream.text();
  } catch {
    return problem(502, "upstream_unreachable", "The RKP API closed the connection.");
  }
  const recording = recordDir();
  if (recording) await record(recording, req.method, apiPath, body, upstream.status, text);
  return new Response(text, {
    status: upstream.status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

export function GET(req: NextRequest, ctx: Context) {
  return forward(req, ctx);
}

export function POST(req: NextRequest, ctx: Context) {
  return forward(req, ctx);
}
