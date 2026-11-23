import { expect, test } from "@playwright/test";
import { idp } from "../playwright.config";

const viewer = { oid: "00000000-0000-0000-0000-0000000000b2", name: "Vic Viewer", login: "vic@example.test", roles: [] as string[] };

test("signing in returns to the page asked for, and reviews are attributed to the user", async ({ page, request }) => {
  await request.post(`${idp}/next-user`, { data: { oid: "00000000-0000-0000-0000-0000000000a1", name: "Ada Admin", login: "ada@example.test", roles: ["RKP.Admin"] } });
  await page.goto("/?workspace=business");
  await expect(page).toHaveURL(/\?workspace=business$/);
  await expect(page.getByRole("button", { name: "Sign out Ada Admin" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Commerce value chain" })).toBeVisible();
  // The placement confirmed in the workspace tests carries the signed-in user, not a shared local account.
  await expect(page.getByText("Confirmed by ada@example.test").first()).toBeVisible();
});

test("without a session nothing is served", async ({ page, playwright }) => {
  const anonymous = await playwright.request.newContext({ baseURL: test.info().project.use.baseURL, maxRedirects: 0 });
  const api = await anonymous.get("/api/rkp/repositories");
  expect(api.status()).toBe(401);
  const home = await anonymous.get("/?workspace=settings");
  expect(home.status()).toBe(303);
  expect(home.headers().location).toContain("/auth/login?return_to=%2F%3Fworkspace%3Dsettings");
  // A forged session cookie is not a session.
  const forged = await anonymous.get("/api/rkp/repositories", { headers: { Cookie: "rkp_session=eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4Iiwicm9sZSI6ImFkbWluIn0." } });
  expect(forged.status()).toBe(401);
  // A callback that does not belong to a sign-in started here is refused.
  await page.goto("/auth/callback?code=abc&state=def");
  await expect(page.getByRole("heading", { name: "Sign-in expired" })).toBeVisible();
  await anonymous.dispose();
});

test("the return address cannot point at another site", async ({ page, request }) => {
  await request.post(`${idp}/next-user`, { data: viewer });
  await page.goto("/auth/login?return_to=//evil.example/steal");
  await expect(page).toHaveURL(/127\.0\.0\.1:\d+\/$/);
});

test("users without the admin role can look but not change", async ({ page, request }) => {
  await request.post(`${idp}/next-user`, { data: viewer });
  await page.goto("/?workspace=business");
  await expect(page.getByRole("button", { name: "Sign out Vic Viewer" })).toBeVisible();
  await expect(page.getByText("Read only")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Commerce value chain" })).toBeVisible();
  const refused = await page.request.post("/api/rkp/business-maps/run-placement", { data: {} });
  expect(refused.status()).toBe(403);
  expect((await refused.json()).message).toContain("read-only");
  // A browser cannot claim a role through the proxy.
  const spoofed = await page.request.post("/api/rkp/business-maps/run-placement", { data: {}, headers: { "X-RKP-Role": "admin", "X-RKP-Actor": "ada@example.test" } });
  expect(spoofed.status()).toBe(403);
});

test("signing out ends the session", async ({ page, request }) => {
  await request.post(`${idp}/next-user`, { data: viewer });
  await page.goto("/");
  await page.getByRole("button", { name: "Sign out Vic Viewer" }).click();
  await expect(page.getByRole("heading", { name: "You are signed out" })).toBeVisible();
  expect((await page.request.get("/api/rkp/repositories")).status()).toBe(401);
});
