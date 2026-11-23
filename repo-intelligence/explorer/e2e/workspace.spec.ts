import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

// The tests share one API and build on each other's map, so they run in order.
test.describe.configure({ mode: "serial" });

/** Compile a local fixture through the same server-side proxy the Explorer uses. */
async function compile(page: Page, file: string) {
  const fixture = await readFile(path.join("test-results", file), "utf8");
  const start = await page.request.post("/api/rkp/repositories", { data: { local_path: fixture, ref: "HEAD" } });
  expect(start.ok()).toBeTruthy();
  const { id } = await start.json();
  await expect.poll(async () => (await (await page.request.get(`/api/rkp/compilations/${id}`)).json()).status).toBe("complete");
}

test("build and review a value chain, then update and reload settings", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await compile(page, "fixture-path");

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Orders fixture" })).toBeVisible();
  await page.getByRole("button", { name: "Business map", exact: true }).click();
  await page.getByRole("button", { name: "Create map" }).click();
  await page.getByLabel("Map name").fill("Commerce value chain");
  await page.getByRole("button", { name: "Add stage" }).click();
  await page.getByLabel("Stage name").fill("Sell");
  await page.getByRole("button", { name: "Add capability" }).click();
  await page.getByLabel("Capability name").fill("Customer management");
  await page.getByLabel("Description", { exact: true }).fill("Manage customer records.");
  await page.getByRole("button", { name: "Add capability" }).click();
  await page.getByLabel("Capability name").last().fill("Order fulfilment");
  await page.getByRole("button", { name: "Save map" }).click();
  await expect(page.getByRole("heading", { name: "Customer management" })).toBeVisible();

  const tray = page.getByRole("listitem").filter({ has: page.getByRole("button", { name: "orders-api", exact: true }) });
  const dropdown = tray.getByRole("combobox", { name: "Capability for orders-api" });
  await dropdown.selectOption({ label: "Customer management" });
  const place = tray.getByRole("button", { name: "Place", exact: true });
  await place.focus();
  await page.keyboard.press("Enter");
  const card = page.getByRole("region", { name: "Customer management", exact: true });
  await expect(card.getByRole("button", { name: "orders-api", exact: true })).toBeVisible();
  await expect(card.getByText("Confirmed by local-admin")).toBeVisible();

  const fulfillment = page.getByRole("region", { name: "Order fulfilment", exact: true });
  await card.getByRole("listitem").dragTo(fulfillment);
  await expect(fulfillment.getByRole("button", { name: "orders-api", exact: true })).toBeVisible();

  // A workspace view has its own address, so a reload stays on it.
  await expect(page).toHaveURL(/workspace=business/);
  await page.reload();
  await expect(page.getByRole("heading", { name: "Commerce value chain" })).toBeVisible();

  // The repository map gains a Purpose card that leads back to the business map.
  await page.getByRole("button", { name: /Orders fixture/ }).click();
  const purpose = page.getByRole("button", { name: /Open business map: 1 capability/ });
  await expect(purpose).toBeVisible();
  await expect(purpose.getByText("Order fulfilment", { exact: true })).toBeVisible();
  await expect(purpose.getByText("00 · PURPOSE")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Snapshots" })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Snapshots" })).toHaveCount(0);
  for (const text of ["Connections", "Repositories", "Languages and extractors", "Decision models", "Business map"]) {
    await expect(page.getByRole("heading", { name: text, exact: true }).last()).toBeVisible();
  }
  await page.getByLabel("Automatically place accepted components").uncheck();
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Workspace settings saved" })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Automatically place accepted components")).not.toBeChecked();
  expect(errors).toEqual([]);
});

test("placement without a model says so, and dependencies are drawn", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await compile(page, "fixture-path-monorepo");

  await page.goto("/?workspace=business");
  await expect(page.getByRole("heading", { name: "Commerce value chain" })).toBeVisible();
  await page.getByRole("button", { name: "Run placement" }).click();
  const status = page.getByRole("status").filter({ hasText: "Automatic placement has no model to use." });
  await expect(status).toBeVisible();
  await expect(status).toContainText("Nothing was placed by AI");

  // Each unplaced component says why, and offers its closest capabilities as one-click placements.
  const api = page.getByRole("listitem").filter({ has: page.getByRole("button", { name: "@acme/api", exact: true }) });
  await expect(api).toContainText("No decision model is configured");
  await api.getByRole("button", { name: "Place @acme/api in Order fulfilment" }).click();
  const web = page.getByRole("listitem").filter({ has: page.getByRole("button", { name: "@acme/web", exact: true }) });
  await web.getByRole("combobox", { name: "Capability for @acme/web" }).selectOption({ label: "Customer management" });
  await web.getByRole("button", { name: "Place", exact: true }).click();

  // The web app calls the API, so its capability depends on the API's: one arrow, and the same in words.
  const customers = page.getByRole("region", { name: "Customer management", exact: true });
  await expect(customers.getByText("Depends on")).toContainText("Order fulfilment");
  await expect(page.locator("path.business-arrow")).toHaveCount(1);

  await status.getByRole("button", { name: "Open decision model settings" }).click();
  await expect(page.getByRole("heading", { name: "Decision models", exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test("edit the map in place and pick starter capabilities", async ({ page }) => {
  await page.goto("/?workspace=business");
  await page.getByRole("button", { name: "Edit map" }).click();
  // Editing keeps the lane layout: every stage is a column with its capabilities inside it.
  const lane = page.getByRole("region", { name: "Sell", exact: true });
  await expect(lane.getByLabel("Capability name").first()).toHaveValue("Customer management");

  await page.getByRole("button", { name: "Starter capabilities" }).click();
  const picker = page.getByRole("region", { name: "Starter capabilities" });
  await picker.getByLabel("Search starter capabilities").fill("logistics");
  await picker.getByLabel(/Logistics & Distribution/).check();
  await picker.getByRole("button", { name: "Add 1 selected" }).click();
  await expect(lane.getByLabel("Capability name").last()).toHaveValue("Logistics & Distribution");
  await page.getByRole("button", { name: "Save map" }).click();
  await expect(page.getByRole("heading", { name: "Logistics & Distribution" })).toBeVisible();
  // Placements made earlier survive a map edit.
  await expect(page.getByRole("region", { name: "Order fulfilment", exact: true }).getByRole("button", { name: "@acme/api", exact: true })).toBeVisible();
});

test("arrange capabilities freely on the canvas", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/?workspace=business");
  await page.getByRole("button", { name: "Edit map" }).click();
  await page.getByRole("button", { name: "Add stage" }).click();
  await page.getByLabel("Stage name").last().fill("Deliver");
  await page.getByRole("button", { name: "Save map" }).click();
  await expect(page.getByRole("heading", { name: "Deliver" })).toBeVisible();

  await page.getByRole("group", { name: "Map layout" }).getByRole("button", { name: "Canvas" }).click();
  const board = page.getByRole("region", { name: "Value chain canvas" });
  await expect(board).toBeVisible();
  await expect(page.getByRole("button", { name: "Canvas" })).toHaveAttribute("aria-pressed", "true");

  // Drag Order fulfilment by its handle into the second column: it moves to that stage and stays where it was dropped.
  const grip = page.getByRole("button", { name: "Move Order fulfilment" });
  await page.setViewportSize({ width: 1280, height: 1100 });
  await grip.scrollIntoViewIfNeeded();
  const from = (await grip.boundingBox())!;
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(from.x + 200, from.y + 60, { steps: 6 });
  await page.mouse.move(from.x + 330, from.y + 90, { steps: 6 });
  await page.mouse.up();
  const deliver = board.getByRole("region", { name: "Deliver", exact: true });
  await expect(deliver.getByRole("region", { name: "Order fulfilment", exact: true })).toBeVisible();
  const card = page.getByRole("region", { name: "Order fulfilment", exact: true });
  const dropped = (await card.boundingBox())!;

  // The arrow follows the card, the component placed in it stays, and the keyboard can nudge it.
  await expect(page.locator("path.business-arrow")).toHaveCount(1);
  await expect(card.getByRole("button", { name: "@acme/api", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Move Order fulfilment" }).focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await expect.poll(async () => Math.round((await card.boundingBox())!.y - dropped.y)).toBe(32);

  // The arrangement is saved with the map.
  await page.waitForResponse((response) => response.url().includes("business-maps/layout") && response.ok());
  await page.reload();
  const after = (await page.getByRole("region", { name: "Order fulfilment", exact: true }).boundingBox())!;
  expect(Math.round(after.x - dropped.x)).toBe(0);
  expect(Math.round(after.y - dropped.y)).toBe(32);

  // Back to lanes: the stage change holds there too.
  await page.getByRole("button", { name: "Lanes" }).click();
  await expect(page.getByRole("region", { name: "Value chain" }).getByRole("heading", { name: "Deliver" })).toBeVisible();
  await expect(page.locator(".business-lane").filter({ hasText: "Deliver" }).getByRole("region", { name: "Order fulfilment", exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test("components stay inside their capability card at phone width", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/?workspace=business");
  await expect(page.getByRole("heading", { name: "Commerce value chain" })).toBeVisible();
  const overflow = await page.evaluate(() => [...document.querySelectorAll<HTMLElement>(".business-capability")].flatMap((card) => {
    const box = card.getBoundingClientRect();
    return [...card.querySelectorAll<HTMLElement>(".business-subject, select, button")]
      .filter((node) => node.getBoundingClientRect().width > 0 && node.getBoundingClientRect().right > box.right + 0.5)
      .map((node) => node.className || node.tagName);
  }));
  expect(overflow).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0);
});
