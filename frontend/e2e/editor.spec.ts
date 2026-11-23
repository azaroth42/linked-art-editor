import { readFileSync } from "node:fs";

import { type Browser, expect, type Locator, type Page, test } from "@playwright/test";

import { login } from "./auth.ts";
import { expectNoAxeViolations } from "./axe.ts";
import { E2E_DEV_USER } from "./devUser.ts";
import { PAINTER_ID, READER_BASE_URI } from "./readerRecords.ts";

// Step 1.17: two browser contexts edit one record through the real `crdt-ws` (playwright.config.ts
// starts it beside the API; the preview server proxies `/ws/` to it). Step 1.18: the profile form
// edits a corpus record and the raw inspector shows exactly the expected JSON-LD.

async function createRecord(
  browser: Browser,
  title: string,
  template: Record<string, unknown> = { identified_by: [{ type: "Name", content: title }] },
): Promise<string> {
  const context = await browser.newContext();
  const response = await context.request.post("/api/records", {
    data: { type: "HumanMadeObject", template },
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

const title = (page: Page) => page.getByLabel("Title 1", { exact: true });

/** Focus a text area with the caret at its start or end (End and Home differ by platform). */
async function caretAt(field: Locator, where: "start" | "end"): Promise<void> {
  await field.focus();
  await field.evaluate((element, end) => {
    const area = element as HTMLTextAreaElement;
    const position = end ? area.value.length : 0;
    area.setSelectionRange(position, position);
  }, where === "end");
}

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
  await caretAt(title(a), "end");
  await title(a).pressSequentially(" by Day");
  await expect(title(b)).toHaveValue("Night Scene by Day");
  await caretAt(title(b), "start");
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

/** The Mona Lisa of the golden corpus (`tests/corpus/linkedart/object-0.json`). */
const MONA_LISA = JSON.parse(
  readFileSync(new URL("../../tests/corpus/linkedart/object-0.json", import.meta.url), "utf8"),
) as Record<string, unknown>;
const AAT = "http://vocab.getty.edu/aat/";
const OIL_PAINT = `${AAT}300015050`;
const CANVAS = `${AAT}300014078`;
const EDTF = "http://id.loc.gov/datatypes/edtf";
const DOURIS = `${READER_BASE_URI}${PAINTER_ID}`;

type Json = Record<string, unknown>;
type MonaLisa = Json & {
  identified_by: Json[];
  made_of: Json[];
  produced_by: Json & { timespan: Json & { identified_by: Json[] } };
};

async function rawJsonld(page: Page): Promise<MonaLisa> {
  return JSON.parse((await page.getByTestId("raw-jsonld").textContent()) ?? "") as MonaLisa;
}

test("a corpus HumanMadeObject is edited through its profile", async ({ browser }) => {
  const id = await createRecord(browser, "", MONA_LISA);
  const page = await openEditor(browser, id);
  const form = page.getByTestId("profile-form");
  const titleField = form.getByLabel("Title 1", { exact: true });
  await expect(titleField).toHaveValue("Mona Lisa");
  await page.getByRole("button", { name: "Show JSON-LD" }).click();
  const before = await rawJsonld(page);

  // Title: the first Name's content.
  await titleField.fill("Portrait of Lisa Gherardini");

  // Materials: oil paint off, canvas on; poplar (a Material not pinned in the set) stays.
  const materials = form.getByRole("group", { name: "Materials" });
  await expect(materials.getByRole("button", { name: "Poplar (wood)" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  const oilPaint = materials.getByRole("button", { name: "oil paint" });
  await expect(oilPaint).toHaveAttribute("aria-pressed", "true");
  await oilPaint.click();
  await materials.getByRole("button", { name: "canvas" }).click();

  // Dates: an EDTF interval.
  await form.getByLabel("Production Date").fill("1503/1519");
  await expect(page.getByTestId("production_date-bounds")).toHaveText(
    "Earliest Jan 1, 1503, latest Dec 31, 1519.",
  );

  // Creator: Leonardo out, Douris (a published Person) in through autocomplete.
  const creator = form.getByRole("group", { name: "Creator" });
  await creator.getByRole("button", { name: "Remove Leonardo da Vinci" }).click();
  await creator.getByRole("combobox").pressSequentially("Dour");
  await page.getByRole("option", { name: /^Douris/ }).click();
  await expect(creator.getByText("Douris", { exact: true })).toBeVisible();

  const expected = structuredClone(before);
  expected.identified_by[0] = {
    ...expected.identified_by[0],
    content: "Portrait of Lisa Gherardini",
  };
  expected.made_of = [
    ...expected.made_of.filter((material) => material.id !== OIL_PAINT),
    { id: CANVAS, type: "Material" },
  ];
  expected.produced_by.carried_out_by = [{ id: DOURIS, type: "Person", _label: "Douris" }];
  const timespan = expected.produced_by.timespan;
  timespan.begin_of_the_begin = "1503-01-01T00:00:00Z";
  timespan.end_of_the_end = "1519-12-31T23:59:59Z";

  await expect(async () => {
    const after = await rawJsonld(page);
    const names = after.produced_by.timespan.identified_by;
    // The EDTF name is new: its internal node IRI is minted under the record URI.
    const minted = names[names.length - 1]?.id;
    expect(minted).toMatch(new RegExp(`^${String(before.id)}/node/[0-9a-f-]{36}$`));
    timespan.identified_by = [
      ...before.produced_by.timespan.identified_by,
      {
        id: minted,
        type: "Name",
        content: "1503/1519",
        classified_as: [{ id: EDTF, type: "Type" }],
      },
    ];
    expect(after).toEqual(expected);
  }).toPass({ timeout: 5_000 });

  await expectNoAxeViolations(page);
  await page.context().close();
});
