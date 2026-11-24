import { expect, test as setup } from "@playwright/test";
import { adminState, idp } from "../playwright.config";

setup("sign in as an administrator", async ({ page, request }) => {
  await request.post(`${idp}/next-user`, { data: { oid: "00000000-0000-0000-0000-0000000000a1", name: "Ada Admin", login: "ada@example.test", roles: ["RKP.Admin"] } });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Sign out Ada Admin" })).toBeVisible();
  await page.context().storageState({ path: adminState });
});
