import { expect, test } from "@playwright/test";

import { expectNoAxeViolations } from "./axe.ts";
import { createRecord, openEditor } from "./editing.ts";

// Step 1.19: the validation drawer and the Publish dialog against the real `lae api` and
// `crdt-ws`: an edit produces a finding, the finding leads to its field, the fix clears it, the
// record is published with its warning accepted, and the new version is public.

test("an edit's finding leads to its field, and the fixed draft is published", async ({
  browser,
}) => {
  const id = await createRecord(browser, "Untitled vase");
  const context = await browser.newContext();
  const page = await context.newPage();
  await openEditor(page, id);
  const drawer = page.getByRole("complementary", { name: "Checks" });
  const title = page.getByTestId("profile-form").getByLabel("Title 1", { exact: true });
  await expect(title).toHaveValue("Untitled vase");

  // Edit: an empty title. The server's rules find it; the finding names its field.
  await title.fill("");
  await drawer.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByTestId("announcer")).toHaveText(
    "Validation finished: 1 error, 1 warning, no notes.",
  );
  const empty = drawer.getByRole("listitem").filter({
    hasText: "Error: This text is empty. Enter a value or remove the entry.",
  });
  await expect(empty).toBeVisible();
  await expectNoAxeViolations(page);

  // Jump to the field and fix it.
  await empty.getByRole("button", { name: "Go to Title" }).click();
  await expect(title).toBeFocused();
  await page.keyboard.type("A Fine Vase");
  await expect(drawer).toContainText("The draft has changed since it was last validated.");
  await drawer.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByTestId("announcer")).toHaveText(
    "Validation finished: no errors, 1 warning, no notes.",
  );
  await expect(drawer.getByRole("listitem")).toHaveCount(1);

  // Publish: the changes are listed; the warning must be accepted first.
  await page.getByRole("button", { name: "Publish…" }).click();
  const dialog = page.getByRole("dialog", { name: "Publish this record" });
  const changes = dialog.getByTestId("change-list");
  await expect(changes).toContainText("Title: added Name “A Fine Vase”");
  const publish = dialog.getByRole("button", { name: "Publish", exact: true });
  await expect(publish).toBeDisabled();
  await dialog
    .getByRole("checkbox", {
      name: "Publish anyway: One of the object's Names should be classified as Primary Name.",
    })
    .check();
  await dialog.getByLabel("Summary of changes (optional)").fill("Give the vase its title");
  await expect(publish).toBeEnabled();
  await expectNoAxeViolations(page);

  const reader = context.waitForEvent("page");
  await publish.click();

  // The public page opens in a new tab; the editor announces the version and goes on with a
  // fresh draft of the new epoch, without the stale-draft dialog.
  const opened = await reader;
  await opened.waitForLoadState();
  expect(new URL(opened.url()).pathname).toBe(`/id/${id}`);
  await expect(opened.locator("body")).toContainText("A Fine Vase");
  await opened.close();

  await expect(page.getByTestId("announcer")).toHaveText("Published version 1.");
  await expect(dialog).toBeHidden();
  await expect(page.getByTestId("connection-status")).toHaveText(/^Connected/);
  await expect(page.getByRole("dialog", { name: "This draft has been replaced" })).toBeHidden();
  await expect(title).toHaveValue("A Fine Vase");
  await expectNoAxeViolations(page);

  const published = await context.request.get(`/id/${id}`, {
    headers: { Accept: "application/ld+json" },
  });
  expect(published.status()).toBe(200);
  const jsonld = (await published.json()) as { identified_by: { content: string }[] };
  expect(jsonld.identified_by.map((name) => name.content)).toEqual(["A Fine Vase"]);
  const revisions = await context.request.get(`/api/records/${id}/revisions`);
  expect(((await revisions.json()) as { summary: string }[]).map((r) => r.summary)).toEqual([
    "Give the vase its title",
  ]);
  await context.close();
});

test("a never-published draft is discarded and its record deleted", async ({ browser }) => {
  const id = await createRecord(browser, "Mistake");
  const page = await (await browser.newContext()).newPage();
  await openEditor(page, id);
  await page.getByRole("button", { name: "Discard draft" }).click();
  const dialog = page.getByRole("dialog", { name: "Discard this draft?" });
  await expect(dialog.getByRole("button", { name: "Keep editing" })).toBeFocused();
  await expectNoAxeViolations(page);
  await dialog.getByRole("button", { name: "Discard", exact: true }).click();
  await expect(page).toHaveURL("/");
  const gone = await page.request.get(`/api/records/${id}`);
  expect(gone.status()).toBe(404);
  await page.context().close();
});
