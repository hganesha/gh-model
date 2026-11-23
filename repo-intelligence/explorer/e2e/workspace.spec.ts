import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

test("build and review a value chain, then update and reload settings", async ({ page }) => {
  const fixture = await readFile(path.join("test-results", "fixture-path"), "utf8");
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));

  // Compile the local fixture through the same server-side proxy the Explorer uses.
  const start = await page.request.post("/api/rkp/repositories", { data: { local_path: fixture, ref: "HEAD" } });
  expect(start.ok()).toBeTruthy();
  const { id } = await start.json();
  await expect.poll(async () => (await (await page.request.get(`/api/rkp/compilations/${id}`)).json()).status).toBe("complete");

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

  await page.reload();
  await expect(page.getByRole("heading", { name: "Purpose" })).toBeVisible();
  await expect(page.getByText("Order fulfilment", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Settings", exact: true }).click();
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
