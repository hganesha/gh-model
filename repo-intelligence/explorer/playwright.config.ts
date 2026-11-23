import { defineConfig } from "@playwright/test";

const token = "rkp-workspace-e2e-token";
const apiPort = process.env.RKP_TEST_API_PORT ?? "18787";
const uiPort = process.env.RKP_TEST_UI_PORT ?? "13100";
const api = `http://127.0.0.1:${apiPort}`;
const ui = `http://127.0.0.1:${uiPort}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  workers: 1,
  use: { baseURL: ui, browserName: "chromium", trace: "retain-on-failure" },
  webServer: [
    {
      command: "../.venv/bin/python e2e/start_api.py",
      url: `${api}/openapi.json`,
      env: { RKP_API_PORT: apiPort, RKP_DEV_TOKEN: token },
      gracefulShutdown: { signal: "SIGTERM", timeout: 8000 },
    },
    {
      command: `npx next dev -H 127.0.0.1 -p ${uiPort}`,
      url: ui,
      env: { RKP_API_URL: api, RKP_DEV_TOKEN: token },
      timeout: 90_000,
    },
  ],
});
