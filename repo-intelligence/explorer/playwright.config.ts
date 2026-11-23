import { defineConfig } from "@playwright/test";

const token = "rkp-workspace-e2e-token";
const apiPort = process.env.RKP_TEST_API_PORT ?? "18787";
const uiPort = process.env.RKP_TEST_UI_PORT ?? "13100";
const idpPort = process.env.RKP_TEST_IDP_PORT ?? "14400";
const api = `http://127.0.0.1:${apiPort}`;
const ui = `http://127.0.0.1:${uiPort}`;
export const idp = `http://127.0.0.1:${idpPort}`;
export const adminState = ".playwright-auth/admin.json";

// The Explorer runs with sign-in on, against a local OpenID Connect provider (e2e/fake_idp.mjs)
// that issues Entra-shaped tokens. The setup project signs in once as an administrator.
const oidc = { client: "rkp-explorer-test", secret: "fake-idp-client-secret" };

export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  workers: 1,
  use: { baseURL: ui, browserName: "chromium", trace: "retain-on-failure" },
  projects: [
    { name: "sign in", testMatch: /auth\.setup\.ts/ },
    { name: "workspace", testMatch: /workspace\.spec\.ts/, dependencies: ["sign in"], use: { storageState: adminState } },
    { name: "sign-in flows", testMatch: /sign-in\.spec\.ts/, dependencies: ["workspace"] },
  ],
  webServer: [
    {
      command: "../.venv/bin/python e2e/start_api.py",
      url: `${api}/openapi.json`,
      env: { RKP_API_PORT: apiPort, RKP_DEV_TOKEN: token },
      gracefulShutdown: { signal: "SIGTERM", timeout: 8000 },
    },
    {
      command: "node e2e/fake_idp.mjs",
      url: `${idp}/health`,
      env: { FAKE_IDP_PORT: idpPort, FAKE_IDP_CLIENT_ID: oidc.client, FAKE_IDP_CLIENT_SECRET: oidc.secret, FAKE_IDP_REDIRECT_URI: `${ui}/auth/callback` },
    },
    {
      command: `npx next dev -H 127.0.0.1 -p ${uiPort}`,
      url: `${ui}/auth/signed-out`,
      env: {
        RKP_API_URL: api,
        RKP_DEV_TOKEN: token,
        RKP_OIDC_ISSUER: idp,
        RKP_OIDC_CLAIMS: "entra",
        RKP_OIDC_CLIENT_ID: oidc.client,
        RKP_OIDC_CLIENT_SECRET: oidc.secret,
        RKP_PUBLIC_URL: ui,
        RKP_SESSION_SECRET: "e2e-session-secret-that-is-at-least-32-chars",
        RKP_OIDC_ADMIN_ROLES: "RKP.Admin",
      },
      timeout: 90_000,
    },
  ],
});
