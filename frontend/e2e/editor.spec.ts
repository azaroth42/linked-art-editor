import { type Browser, expect, type Page, test } from "@playwright/test";

import { login } from "./auth.ts";
import { expectNoAxeViolations } from "./axe.ts";
import { E2E_DEV_USER } from "./devUser.ts";

// Step 1.17: two browser contexts edit one record through the real `crdt-ws` (playwright.config.ts
// starts it beside the API; the preview server proxies `/ws/` to it).

async function createRecord(browser: Browser, title: string): Promise<string> {
  const context = await browser.newContext();
  const response = await context.request.post("/api/records", {
    data: {
      type: "HumanMadeObject",
      template: { identified_by: [{ type: "Name", content: title }] },
    },
  });
  expect(response.status()).toBe(201);
  const created = (await response.json()) as { id: string; draft_epoch: number };
  const record = await context.request.get(`/api/records/${created.id}`);
  expect(record.headers()["x-draft-epoch"]).toBe(String(created.draft_epoch));
  await context.close();
  return created.id;
}

async function openEditor(browser: Browser, id: string): Promise<Page> {
  const page = await (await browser.newContext()).newPage();
  await login(page, `/edit/${id}`);
  await expect(page.getByTestId("connection-status")).toHaveText(/^Connected/);
  return page;
}

const title = (page: Page) => page.getByLabel("Title");

test("two windows see each other's edits and presence", async ({ browser }) => {
  const id = await createRecord(browser, "Night Scene");
  const a = await openEditor(browser, id);
  const b = await openEditor(browser, id);

  for (const page of [a, b]) {
    await expect(title(page)).toHaveValue("Night Scene");
    await expect(page.getByTestId("presence").getByRole("listitem")).toHaveText([
      E2E_DEV_USER.email,
    ]);
  }

  // A appends, B prepends: both edits survive on both sides (one Y.Text splice each).
  await title(a).press("End");
  await title(a).pressSequentially(" by Day");
  await expect(title(b)).toHaveValue("Night Scene by Day");
  await title(b).press("Home");
  await title(b).pressSequentially("A ");
  for (const page of [a, b]) {
    await expect(title(page)).toHaveValue("A Night Scene by Day");
  }

  // Undo in A reverts only A's own edit, on both sides.
  await a.getByRole("button", { name: "Undo" }).click();
  for (const page of [a, b]) {
    await expect(title(page)).toHaveValue("A Night Scene");
  }

  // The raw inspector shows the projected JSON-LD of the shared draft.
  await b.getByRole("button", { name: "Show JSON-LD" }).click();
  await expect(b.getByTestId("raw-jsonld")).toContainText('"content": "A Night Scene"');
  await expectNoAxeViolations(b);

  // When B leaves, A's presence list empties.
  await b.context().close();
  await expect(a.getByTestId("presence")).toHaveText("Nobody else has this draft open.");
  await expectNoAxeViolations(a);
  await a.context().close();
});

test("a stale epoch shows the blocking reload dialog", async ({ browser }) => {
  const id = await createRecord(browser, "Stale");
  const page = await (await browser.newContext()).newPage();
  // Answer the record with an epoch the room does not have: crdt-ws closes with 4409.
  await page.route(`**/api/records/${id}`, async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, headers: { ...response.headers(), "x-draft-epoch": "999" } });
  });
  await login(page, `/edit/${id}`);
  const dialog = page.getByRole("dialog", { name: "This draft has been replaced" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Reload" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expectNoAxeViolations(page);
  await page.context().close();
});
