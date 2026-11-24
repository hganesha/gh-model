import "server-only";
import { createRemoteJWKSet, jwtVerify, SignJWT, type JWTPayload } from "jose";

// Sign-in for the Explorer with any OpenID Connect provider; Microsoft Entra ID is the
// default adapter. The Explorer is the only public part of RKP, so the login lives here:
// authorization code flow with PKCE, a nonce and a signed state cookie; the ID token is
// checked against the provider's published keys, and the session is an HMAC-signed
// cookie. The internal API is told who is acting through the proxy (X-RKP-Actor/-Role).
//
// Off in `next dev` unless an issuer is configured. A production build refuses every request
// without one, unless RKP_ALLOW_ANONYMOUS=1 says another control restricts access.
// Settings:
//   RKP_ENTRA_TENANT_ID     Entra tenant (GUID or domain); sets the issuer for you, or
//   RKP_OIDC_ISSUER         any OIDC issuer URL
//   RKP_OIDC_CLIENT_ID, RKP_OIDC_CLIENT_SECRET   the app registration
//   RKP_PUBLIC_URL          the Explorer's public origin; redirect URIs are
//                           <url>/auth/callback and <url>/auth/signed-out
//   RKP_SESSION_SECRET      at least 32 characters; signs the session cookie
//   RKP_OIDC_CLAIMS=entra   read Entra's oid/tid for the user id with a non-Entra issuer URL
//   RKP_OIDC_ADMIN_ROLES    optional, comma-separated app roles or group IDs that may
//                           change things; everyone else signs in read-only. Empty: all
//                           signed-in users can change things.

export type Role = "admin" | "viewer";
export type SessionUser = { id: string; name: string; login: string; role: Role };

export type AuthConfig = {
  issuer: string;
  clientId: string;
  clientSecret: string;
  publicUrl: string;
  secret: Uint8Array;
  adminRoles: string[];
  entra: boolean;
  secure: boolean;
};

const SESSION_TTL_SECONDS = 8 * 60 * 60;
const STATE_TTL_SECONDS = 10 * 60;
const AUDIENCE = "rkp-explorer";

/** https, or plain http on this machine only (a local test identity provider). */
export function secureUrl(value: unknown): value is string {
  if (typeof value !== "string") return false;
  try {
    const u = new URL(value);
    return u.protocol === "https:" || (u.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(u.hostname));
  } catch {
    return false;
  }
}

export class AuthMisconfigured extends Error {}

/** null when sign-in is off. Throws AuthMisconfigured when it is half set up, so the Explorer fails closed. */
export function authConfig(env: NodeJS.ProcessEnv = process.env): AuthConfig | null {
  const tenant = env.RKP_ENTRA_TENANT_ID?.trim();
  const issuer = (env.RKP_OIDC_ISSUER?.trim() || (tenant ? `https://login.microsoftonline.com/${tenant}/v2.0` : "")).replace(/\/+$/, "");
  if (!issuer) {
    // A production build serves no one without sign-in, unless the operator says another control
    // (an IP allow-list, a private network) stands in for it. `next dev` stays open for local use.
    if (env.NODE_ENV === "production" && env.RKP_ALLOW_ANONYMOUS !== "1") {
      throw new AuthMisconfigured("Sign-in is required. Set RKP_ENTRA_TENANT_ID or RKP_OIDC_ISSUER, or RKP_ALLOW_ANONYMOUS=1 if another control such as an IP allow-list restricts access.");
    }
    return null;
  }
  const missing = ["RKP_OIDC_CLIENT_ID", "RKP_OIDC_CLIENT_SECRET", "RKP_PUBLIC_URL", "RKP_SESSION_SECRET"].filter((k) => !env[k]?.trim());
  if (missing.length) throw new AuthMisconfigured(`Sign-in is configured but ${missing.join(", ")} ${missing.length > 1 ? "are" : "is"} not set.`);
  const secret = env.RKP_SESSION_SECRET!.trim();
  if (secret.length < 32) throw new AuthMisconfigured("RKP_SESSION_SECRET must be at least 32 characters.");
  let publicUrl: URL;
  try {
    publicUrl = new URL(env.RKP_PUBLIC_URL!.trim());
  } catch {
    throw new AuthMisconfigured("RKP_PUBLIC_URL is not a URL.");
  }
  if (!secureUrl(issuer)) throw new AuthMisconfigured("The OIDC issuer must use https.");
  return {
    issuer,
    clientId: env.RKP_OIDC_CLIENT_ID!.trim(),
    clientSecret: env.RKP_OIDC_CLIENT_SECRET!.trim(),
    publicUrl: publicUrl.origin,
    secret: new TextEncoder().encode(secret),
    adminRoles: (env.RKP_OIDC_ADMIN_ROLES ?? "").split(",").map((r) => r.trim()).filter(Boolean),
    entra: Boolean(tenant) || issuer.startsWith("https://login.microsoftonline.com/") || env.RKP_OIDC_CLAIMS === "entra",
    secure: publicUrl.protocol === "https:",
  };
}

// __Host- cookies must be Secure, host-only and on path /, which pins them to this origin.
export const sessionCookie = (c: AuthConfig) => (c.secure ? "__Host-rkp_session" : "rkp_session");
export const stateCookie = (c: AuthConfig) => (c.secure ? "__Host-rkp_oidc" : "rkp_oidc");
export const cookieOptions = (c: AuthConfig, maxAge: number) => ({ httpOnly: true, secure: c.secure, sameSite: "lax" as const, path: "/", maxAge });
export const SESSION_MAX_AGE = SESSION_TTL_SECONDS;
export const STATE_MAX_AGE = STATE_TTL_SECONDS;

type Discovery = { issuer: string; authorization_endpoint: string; token_endpoint: string; jwks_uri: string; end_session_endpoint?: string };
const discoveries = new Map<string, Promise<Discovery>>();
const keySets = new Map<string, ReturnType<typeof createRemoteJWKSet>>();

export function discover(c: AuthConfig): Promise<Discovery> {
  let pending = discoveries.get(c.issuer);
  if (!pending) {
    pending = (async () => {
      const res = await fetch(`${c.issuer}/.well-known/openid-configuration`, { headers: { Accept: "application/json" }, cache: "no-store", signal: AbortSignal.timeout(10_000) });
      if (!res.ok) throw new Error(`discovery returned HTTP ${res.status}`);
      const doc = (await res.json()) as Partial<Discovery>;
      for (const field of ["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"] as const) {
        if (!secureUrl(doc[field])) throw new Error(`discovery document has no https ${field}`);
      }
      if (doc.issuer!.replace(/\/+$/, "") !== c.issuer) throw new Error("discovery issuer does not match the configured issuer");
      return doc as Discovery;
    })();
    pending.catch(() => discoveries.delete(c.issuer)); // retry on the next sign-in
    discoveries.set(c.issuer, pending);
  }
  return pending;
}

const sign = (c: AuthConfig, claims: JWTPayload, subject: string, ttl: number, type: string) =>
  new SignJWT({ ...claims, typ: type }).setProtectedHeader({ alg: "HS256" }).setSubject(subject).setIssuedAt().setExpirationTime(`${ttl}s`).setAudience(AUDIENCE).setIssuer(AUDIENCE).sign(c.secret);

async function unsign(c: AuthConfig, token: string | undefined, type: string): Promise<JWTPayload | null> {
  if (!token) return null;
  try {
    const { payload } = await jwtVerify(token, c.secret, { algorithms: ["HS256"], audience: AUDIENCE, issuer: AUDIENCE, requiredClaims: ["sub", "exp", "iat"] });
    return payload.typ === type ? payload : null;
  } catch {
    return null;
  }
}

export type LoginState = { state: string; nonce: string; verifier: string; returnTo: string };

export const signState = (c: AuthConfig, s: LoginState) => sign(c, { ...s }, "login", STATE_TTL_SECONDS, "state");

export async function readState(c: AuthConfig, token: string | undefined): Promise<LoginState | null> {
  const p = await unsign(c, token, "state");
  if (!p || typeof p.state !== "string" || typeof p.nonce !== "string" || typeof p.verifier !== "string") return null;
  return { state: p.state, nonce: p.nonce, verifier: p.verifier, returnTo: safeReturnPath(p.returnTo) };
}

export const signSession = (c: AuthConfig, u: SessionUser) => sign(c, { name: u.name, login: u.login, role: u.role }, u.id, SESSION_TTL_SECONDS, "session");

export async function readSession(c: AuthConfig, token: string | undefined): Promise<SessionUser | null> {
  const p = await unsign(c, token, "session");
  if (!p?.sub || (p.role !== "admin" && p.role !== "viewer")) return null;
  return { id: p.sub, name: String(p.name ?? ""), login: String(p.login ?? ""), role: p.role };
}

/** Only same-site absolute paths, so the return address can never send the user elsewhere. */
export function safeReturnPath(value: unknown): string {
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") && !value.startsWith("/\\") && !value.startsWith("/auth/") ? value : "/";
}

export const randomToken = () => Buffer.from(crypto.getRandomValues(new Uint8Array(32))).toString("base64url");

export async function pkceChallenge(verifier: string): Promise<string> {
  return Buffer.from(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))).toString("base64url");
}

/** Exchange the authorization code and verify the ID token: signature, issuer, audience, lifetime, nonce. */
export async function completeLogin(c: AuthConfig, code: string, login: LoginState): Promise<SessionUser> {
  const d = await discover(c);
  const res = await fetch(d.token_endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: `${c.publicUrl}/auth/callback`,
      client_id: c.clientId,
      client_secret: c.clientSecret,
      code_verifier: login.verifier,
    }),
    cache: "no-store",
    signal: AbortSignal.timeout(15_000),
  });
  if (!res.ok) throw new Error(`the identity provider refused the sign-in (HTTP ${res.status})`);
  const idToken = ((await res.json()) as { id_token?: unknown }).id_token;
  if (typeof idToken !== "string") throw new Error("the identity provider returned no ID token");
  let keys = keySets.get(d.jwks_uri);
  if (!keys) keySets.set(d.jwks_uri, (keys = createRemoteJWKSet(new URL(d.jwks_uri))));
  const { payload } = await jwtVerify(idToken, keys, {
    issuer: d.issuer,
    audience: c.clientId,
    algorithms: ["RS256", "RS384", "RS512", "PS256", "ES256", "ES384"],
    requiredClaims: ["sub", "exp", "iat", "nonce"],
    clockTolerance: 30,
  });
  if (payload.nonce !== login.nonce) throw new Error("the sign-in response does not belong to this sign-in");
  return userFromClaims(c, payload);
}

const text = (v: unknown) => (typeof v === "string" ? v.trim() : "");
const list = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : typeof v === "string" ? [v] : []);

/** Map ID token claims to a user. Entra: the stable id is tenant + object id (`sub` differs per app). */
export function userFromClaims(c: AuthConfig, claims: JWTPayload): SessionUser {
  const oid = text(claims.oid), tid = text(claims.tid);
  const id = c.entra && oid && tid ? `entra:${tid}:${oid}` : `oidc:${text(claims.sub)}`;
  const login = text(claims.preferred_username) || text(claims.email) || text(claims.upn);
  const name = text(claims.name) || login || id;
  // Entra puts app roles in `roles` and group object IDs in `groups`; other providers often use `groups` for names.
  const held = new Set([...list(claims.roles), ...list(claims.groups)]);
  const role: Role = c.adminRoles.length === 0 || c.adminRoles.some((r) => held.has(r)) ? "admin" : "viewer";
  return { id, name, login, role };
}

/** The value sent to the API as X-RKP-Actor and recorded on reviews: printable ASCII, at most 200 characters. */
export function actorOf(u: SessionUser): string {
  return (u.login || u.name || u.id).replace(/[^\x20-\x7e]/g, "").slice(0, 200) || u.id.slice(0, 200);
}
