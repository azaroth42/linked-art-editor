import { type APIRequestContext, expect, test } from "@playwright/test";

import { expectNoAxeViolations } from "./axe.ts";
import { createRecord, openEditor, publishDraft } from "./editing.ts";

// Step 1.20: the Revision History Browser against the real `lae api` and `crdt-ws`. A record is
// published twice; the history lists both versions and shows the title change in either
// direction; reverting to version 1 opens the editor on a new draft equal to version 1 with
// nothing published; publishing that draft makes version 3, equal to version 1.

interface Diff {
  changes: unknown[];
}

async function json<T>(request: APIRequestContext, method: "GET" | "POST", url: string) {
  const response = method === "GET" ? await request.get(url) : await request.post(url);
  expect(response.status(), url).toBe(200);
  return (await response.json()) as T;
}

async function publicTitles(request: APIRequestContext, id: string): Promise<string[]> {
  const response = await request.get(`/id/${id}`, { headers: { Accept: "application/ld+json" } });
  expect(response.status()).toBe(200);
  const jsonld = (await response.json()) as { identified_by: { content: string }[] };
  return jsonld.identified_by.map((name) => name.content);
}

test("two versions are compared, the first is restored by revert, and publishing it makes a third", async ({
  browser,
}) => {
  const id = await createRecord(browser, "Vase");
  const context = await browser.newContext();
  const page = await context.newPage();
  const request = context.request;
  await openEditor(page, id);
  const title = page.getByTestId("profile-form").getByLabel("Title 1", { exact: true });
  await expect(title).toHaveValue("Vase");
  await publishDraft(page, "First version", 1);
  await expect(title).toHaveValue("Vase");
  await title.fill("Amphora");
  await publishDraft(page, "Call it an amphora", 2);

  // The history: both versions, newest first; the latest two are compared.
  await page.getByRole("link", { name: "Revision history" }).click();
  await expect(page).toHaveURL(`/records/${id}/history`);
  const table = page.getByRole("table", { name: "2 published versions" });
  await expect(table.getByRole("rowheader")).toHaveText(["Version 2 (current)", "Version 1"]);
  await expect(table.getByRole("row").filter({ hasText: "Version 2 (current)" })).toContainText(
    "Call it an amphora",
  );
  await expect(
    table.getByRole("link", { name: "View version 1 (opens in a new tab)" }),
  ).toHaveAttribute("href", `/id/${id}?version=1`);
  const forward = page.getByRole("region", { name: "Changes from version 1 to version 2" });
  await expect(forward.getByTestId("change-list")).toHaveText(
    /Title: changed from “Vase” to “Amphora”/,
  );
  await expectNoAxeViolations(page);

  // The other direction.
  await forward.getByRole("button", { name: "Swap the versions" }).click();
  const backward = page.getByRole("region", { name: "Changes from version 2 to version 1" });
  await expect(backward.getByTestId("change-list")).toHaveText(
    /Title: changed from “Amphora” to “Vase”/,
  );
  await expect(page.getByRole("radio", { name: "Compare from version 2" })).toBeChecked();
  await expect(page.getByRole("radio", { name: "Compare to version 1" })).toBeChecked();

  // Revert to version 1: asked first, then the editor opens on the new draft.
  await table.getByRole("button", { name: "Revert to version 1" }).click();
  const dialog = page.getByRole("dialog", { name: "Revert to version 1?" });
  await expect(dialog).toContainText("Nothing is published");
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  await expectNoAxeViolations(page);
  await dialog.getByRole("button", { name: "Revert", exact: true }).click();

  await expect(page).toHaveURL(`/edit/${id}`);
  await expect(page.getByTestId("reverted")).toHaveText(
    "The draft now holds version 1. Nothing is published until you publish it.",
  );
  await expect(page.getByTestId("connection-status")).toHaveText(/^Connected/);
  await expect(title).toHaveValue("Vase");
  await expect(page.getByRole("dialog", { name: "This draft has been replaced" })).toBeHidden();
  await expectNoAxeViolations(page);

  // Nothing is published, and the draft equals version 1: its changes since version 2 are
  // exactly those from version 2 to version 1.
  expect(await publicTitles(request, id)).toEqual(["Amphora"]);
  expect(await json<unknown[]>(request, "GET", `/api/records/${id}/revisions`)).toHaveLength(2);
  const draftDiff = await json<Diff>(request, "POST", `/api/records/${id}/draft/diff`);
  const revisionDiff = await json<Diff>(request, "GET", `/api/records/${id}/revisions/2/diff/1`);
  expect(revisionDiff.changes).toHaveLength(1);
  expect(draftDiff.changes).toEqual(revisionDiff.changes);

  // Publishing the reverted draft makes version 3, the same as version 1.
  await publishDraft(page, "Back to the first title", 3);
  await expect(page.getByTestId("reverted")).toBeHidden();
  expect(await publicTitles(request, id)).toEqual(["Vase"]);
  const same = await json<Diff>(request, "GET", `/api/records/${id}/revisions/1/diff/3`);
  expect(same.changes).toEqual([]);
  await context.close();
});
