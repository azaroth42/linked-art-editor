import { expect, test } from "@playwright/test";

import { expectNoAxeViolations } from "./axe.ts";

test("home route renders a translated string and has no axe violations", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: "Linked Art Editor" })).toBeVisible();
  await expectNoAxeViolations(page);
});
