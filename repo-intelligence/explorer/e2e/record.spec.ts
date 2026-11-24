import { expect, test, type Page } from "@playwright/test";
import { readdir, readFile, realpath, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { visitRepository } from "./views";

// Records the API responses behind the Explorer's read-only views of the F01 and F02 fixtures into
// e2e/recorded (GI-030). Run with:  RKP_RECORD=1 npx playwright test --project=record
// The replay project then builds the Explorer for production and renders the same views from them.

const recorded = path.join("e2e", "recorded");
const VIEWS = ["Graph", "Timeline", "Quality", "Decisions", "Map"];

async function compile(page: Page, file: string): Promise<string> {
  const fixture = await readFile(path.join("test-results", file), "utf8");
  const start = await page.request.post("/api/rkp/repositories", { data: { local_path: fixture, ref: "HEAD" } });
  expect(start.ok()).toBeTruthy();
  const { id } = await start.json();
  await expect.poll(async () => (await (await page.request.get(`/api/rkp/compilations/${id}`)).json()).status, { timeout: 60_000 }).toBe("complete");
  return fixture;
}


test("record the read-only views of the fixture repositories", async ({ page }) => {
  test.skip(!process.env.RKP_RECORD, "recording runs only with RKP_RECORD=1");
  test.setTimeout(180_000);
  await rm(recorded, { recursive: true, force: true });
  const fixtures = [await compile(page, "fixture-path"), await compile(page, "fixture-path-monorepo")];
  // Drop the compile calls themselves: only what the views read is kept.
  await rm(recorded, { recursive: true, force: true });
  await visitRepository(page, "Orders fixture", VIEWS);
  await visitRepository(page, "Storefront fixture", VIEWS);

  // Recorded answers must not carry this machine's temporary paths.
  const roots = new Set<string>();
  for (const f of fixtures) {
    roots.add(path.dirname(f));
    roots.add(path.dirname(await realpath(f)));
  }
  for (const file of await readdir(recorded)) {
    const full = path.join(recorded, file);
    let text = await readFile(full, "utf8");
    for (const root of [...roots].sort((a, b) => b.length - a.length)) text = text.split(root).join("/fixtures");
    await writeFile(full, text);
  }
  expect((await readdir(recorded)).length).toBeGreaterThan(10);
});
