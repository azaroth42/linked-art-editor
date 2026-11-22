import { expect, type Page } from "@playwright/test";

import { E2E_DEV_USER } from "./devUser.ts";

/**
 * Log in as the dev user: with `LAE_DEV_USER` set on the API every request is that user, so the
 * login page forwards straight to `next` and the header names the user.
 */
export async function login(page: Page, next = "/"): Promise<void> {
  await page.goto(`/login?${new URLSearchParams({ next }).toString()}`);
  await expect(page).toHaveURL(next);
  await expect(page.getByTestId("current-user")).toContainText(E2E_DEV_USER.email);
}
