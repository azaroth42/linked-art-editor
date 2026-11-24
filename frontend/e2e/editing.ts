import { type Browser, expect, type Page } from "@playwright/test";

import { login } from "./auth.ts";

/**
 * Create a `HumanMadeObject` titled `title` through the API and return its id. The root
 * `_label` is derived from the name (Step 1.19a).
 */
export async function createRecord(browser: Browser, title: string): Promise<string> {
  const context = await browser.newContext();
  const response = await context.request.post("/api/records", {
    data: {
      type: "HumanMadeObject",
      template: { identified_by: [{ type: "Name", content: title }] },
    },
  });
  expect(response.status()).toBe(201);
  const created = (await response.json()) as { id: string };
  await context.close();
  return created.id;
}

/** Sign in, open the record's editor and wait until its draft is live. */
export async function openEditor(page: Page, id: string): Promise<void> {
  await login(page, `/edit/${id}`);
  await expect(page.getByTestId("connection-status")).toHaveText(/^Connected/);
}

/**
 * Publish the open editor's draft through the Publish dialog, accepting every warning, and wait
 * until the editor is live again on the next draft. The public page the editor opens in a new tab
 * is closed.
 */
export async function publishDraft(page: Page, summary: string, version: number): Promise<void> {
  await page.getByRole("button", { name: "Publish…" }).click();
  const dialog = page.getByRole("dialog", { name: "Publish this record" });
  const summaryField = dialog.getByLabel("Summary of changes (optional)");
  await expect(summaryField).toBeVisible();
  for (const warning of await dialog.getByRole("checkbox").all()) {
    await warning.check();
  }
  await summaryField.fill(summary);
  const reader = page.context().waitForEvent("page");
  await dialog.getByRole("button", { name: "Publish", exact: true }).click();
  await (await reader).close();
  await expect(page.getByTestId("announcer")).toHaveText(`Published version ${String(version)}.`);
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("connection-status")).toHaveText(/^Connected/);
}
