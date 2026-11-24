import { expect, test } from "@playwright/test";
import { replayUrl, strictUrl } from "../playwright.config";
import { visitRepository } from "./views";

// A production build of the Explorer, with no API at all, renders the fixture repositories and
// every plane from recorded responses (GI-030), and the same build refuses to serve without sign-in.

test.use({ baseURL: replayUrl });

test("a production build renders every plane from recorded fixture responses", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const missing: string[] = [];
  page.on("response", (res) => { if (res.status() === 404 && res.url().includes("/api/rkp/")) missing.push(res.url()); });

  for (const name of ["Orders fixture", "Storefront fixture"]) {
    await visitRepository(page, name, ["Quality"]);
    await expect(page.getByText(/Files parsed/).first()).toBeVisible();
  }
  expect(missing).toEqual([]);
  expect(errors).toEqual([]);
});

test("a replaying Explorer cannot change anything", async ({ request }) => {
  const write = await request.post("/api/rkp/repositories", { data: { url: "hganesha/example", ref: "main" } });
  expect(write.status()).toBe(404);
  expect((await write.json()).code).toBe("not_recorded");
});

test("a production build without sign-in or an explicit opt-out serves nothing", async ({ request }) => {
  for (const target of ["/", "/api/rkp/repositories"]) {
    const res = await request.get(strictUrl + target, { maxRedirects: 0 });
    expect(res.status()).toBe(500);
    expect(await res.text()).toContain("Sign-in is required");
  }
});
