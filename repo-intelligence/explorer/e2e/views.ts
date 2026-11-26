import { expect, type Page } from "@playwright/test";

export const PLANE_TITLES = ["Architecture", "Contracts", "Data", "Jobs", "AI integrations", "Tests", "Deployment", "Experience", "Business", "Security"];

/** Resolves once no request to the API proxy has been in flight for a moment. */
export function apiSettled(page: Page): () => Promise<void> {
  let pending = 0;
  const done = (url: string) => { if (url.includes("/api/rkp/")) pending = Math.max(0, pending - 1); };
  page.on("request", (r) => { if (r.url().includes("/api/rkp/")) pending++; });
  page.on("requestfinished", (r) => done(r.url()));
  page.on("requestfailed", (r) => done(r.url()));
  return async () => {
    await expect.poll(() => pending, { intervals: [100, 250, 250] }).toBe(0);
    await page.waitForTimeout(250);
    await expect.poll(() => pending, { intervals: [100, 250] }).toBe(0);
  };
}

/** Open a repository, each of its planes, and the given views, waiting for every read to finish. */
export async function visitRepository(page: Page, name: string, views: string[]) {
  const settled = apiSettled(page);
  await page.goto("/");
  await page.getByRole("button", { name: new RegExp(name) }).first().click();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  await settled();
  for (const title of PLANE_TITLES) {
    await page.getByRole("tab", { name: "Map" }).click();
    await settled();
    await page.getByRole("heading", { name: title, exact: true }).click();
    await settled();
  }
  for (const view of views) {
    await page.getByRole("tab", { name: view }).click();
    await settled();
  }
}
