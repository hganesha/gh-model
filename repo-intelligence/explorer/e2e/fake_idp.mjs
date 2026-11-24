// A minimal OpenID Connect provider for the sign-in tests. It signs in whoever was last
// set with POST /next-user, issues Entra-shaped ID tokens (oid, tid, roles), and checks
// what a real provider checks: client id and secret, redirect URI and the PKCE verifier.
import { createHash, randomBytes } from "node:crypto";
import { createServer } from "node:http";
import { exportJWK, generateKeyPair, SignJWT } from "jose";

const port = Number(process.env.FAKE_IDP_PORT ?? 14400);
const issuer = `http://127.0.0.1:${port}`;
const clientId = process.env.FAKE_IDP_CLIENT_ID ?? "rkp-explorer-test";
const clientSecret = process.env.FAKE_IDP_CLIENT_SECRET ?? "fake-idp-client-secret";
const redirectUri = process.env.FAKE_IDP_REDIRECT_URI;

const { publicKey, privateKey } = await generateKeyPair("RS256");
const jwk = { ...(await exportJWK(publicKey)), kid: "test", use: "sig", alg: "RS256" };
const codes = new Map();
let nextUser = { oid: "00000000-0000-0000-0000-0000000000a1", name: "Ada Admin", login: "ada@example.test", roles: ["RKP.Admin"] };

const json = (res, status, body) => { res.writeHead(status, { "Content-Type": "application/json" }); res.end(JSON.stringify(body)); };
const body = (req) => new Promise((resolve) => { let s = ""; req.on("data", (c) => (s += c)); req.on("end", () => resolve(s)); });

createServer(async (req, res) => {
  const url = new URL(req.url, issuer);
  if (url.pathname === "/.well-known/openid-configuration") {
    return json(res, 200, { issuer, authorization_endpoint: `${issuer}/authorize`, token_endpoint: `${issuer}/token`, jwks_uri: `${issuer}/jwks`, end_session_endpoint: `${issuer}/logout` });
  }
  if (url.pathname === "/jwks") return json(res, 200, { keys: [jwk] });
  if (url.pathname === "/next-user" && req.method === "POST") {
    nextUser = { ...nextUser, ...JSON.parse(await body(req)) };
    return json(res, 200, nextUser);
  }
  if (url.pathname === "/authorize") {
    const q = url.searchParams;
    if (q.get("client_id") !== clientId || q.get("redirect_uri") !== redirectUri || q.get("code_challenge_method") !== "S256" || !q.get("code_challenge") || !q.get("nonce")) {
      return json(res, 400, { error: "invalid_request" });
    }
    const code = randomBytes(16).toString("hex");
    codes.set(code, { challenge: q.get("code_challenge"), nonce: q.get("nonce"), user: nextUser });
    const back = new URL(redirectUri);
    back.searchParams.set("code", code);
    back.searchParams.set("state", q.get("state"));
    res.writeHead(302, { Location: back.toString() });
    return res.end();
  }
  if (url.pathname === "/token" && req.method === "POST") {
    const form = new URLSearchParams(await body(req));
    const grant = codes.get(form.get("code"));
    codes.delete(form.get("code"));
    const verified = grant && createHash("sha256").update(form.get("code_verifier") ?? "").digest("base64url") === grant.challenge;
    if (!verified || form.get("client_id") !== clientId || form.get("client_secret") !== clientSecret || form.get("redirect_uri") !== redirectUri) {
      return json(res, 400, { error: "invalid_grant" });
    }
    const { user } = grant;
    const idToken = await new SignJWT({ nonce: grant.nonce, oid: user.oid, tid: "11111111-1111-1111-1111-111111111111", name: user.name, preferred_username: user.login, roles: user.roles })
      .setProtectedHeader({ alg: "RS256", kid: "test" }).setIssuer(issuer).setAudience(clientId).setSubject(`pairwise-${user.oid}`).setIssuedAt().setExpirationTime("5m").sign(privateKey);
    return json(res, 200, { token_type: "Bearer", id_token: idToken, access_token: "unused" });
  }
  if (url.pathname === "/logout") {
    res.writeHead(302, { Location: url.searchParams.get("post_logout_redirect_uri") ?? "/" });
    return res.end();
  }
  if (url.pathname === "/health") return json(res, 200, { ok: true });
  json(res, 404, { error: "not_found" });
}).listen(port, "127.0.0.1");
