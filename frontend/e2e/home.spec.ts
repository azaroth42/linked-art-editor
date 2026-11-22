import { expect, test } from "@playwright/test";

import { login } from "./auth.ts";
import { expectNoAxeViolations } from "./axe.ts";
import { E2E_DEV_USER } from "./devUser.ts";

test("the dev user logs in, opens / and the page has no axe violations", async ({ page }) => {
  await login(page);
  await expect(page.getByRole("heading", { level: 1, name: "Linked Art Editor" })).toBeVisible();
  await expect(page.getByTestId("current-user")).toHaveText(
    `Signed in as ${E2E_DEV_USER.email} (${E2E_DEV_USER.role})`,
  );
  await expect(page).toHaveTitle("Linked Art Editor");
  await expectNoAxeViolations(page);
});

test("the API answers through the preview server's proxy as the dev user", async ({ page }) => {
  const response = await page.request.get("/auth/me");
  expect(response.status()).toBe(200);
  const me = (await response.json()) as { display_name: string; permissions: string[] };
  expect(me.display_name).toBe(E2E_DEV_USER.email);
  expect(me.permissions).toContain("publish");
});

test("the skip link moves keyboard focus to the main content", async ({ page }) => {
  await login(page);
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to main content" });
  await expect(skip).toBeFocused();
  await expect(skip).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main")).toBeFocused();
});
