import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { type Browser, expect, type Locator, type Page, test } from "@playwright/test";

import { expectNoAxeViolations } from "./axe.ts";
import { openEditor, publishDraft } from "./editing.ts";

// Step 1.22 (Phase 1 exit gate): three browser contexts edit one record through the real
// `crdt-ws` for 60 seconds, all at the same time: A extends the title, B toggles a material,
// C changes a dimension's value and toggles another material (so B and C write the same `made_of`
// array concurrently). Then A publishes. Nothing may be lost: the published JSON-LD holds every
// editor's last state, and it passes the corpus round-trip invariant (`scripts/check_roundtrip.py`,
// the oracle of `backend/tests/test_roundtrip.py`).

const SESSION_MS = 60_000;
const AAT = "http://vocab.getty.edu/aat/";
const CANVAS = `${AAT}300014078`;
const OIL_PAINT = `${AAT}300015050`;
const HEIGHT = `${AAT}300055644`;
const CENTIMETERS = `${AAT}300379098`;
const TITLE = "Collaboration vase";
const FIRST_VALUE = 10;

const BACKEND = fileURLToPath(new URL("../../backend", import.meta.url));
const ROUNDTRIP = fileURLToPath(new URL("../../scripts/check_roundtrip.py", import.meta.url));

interface Published {
  identified_by: { content: string }[];
  made_of?: { id: string }[];
  dimension: { value: number; unit: { id: string }; classified_as: { id: string }[] }[];
}

async function createRecord(browser: Browser): Promise<string> {
  const context = await browser.newContext();
  const response = await context.request.post("/api/records", {
    data: {
      type: "HumanMadeObject",
      template: {
        _label: TITLE,
        identified_by: [{ type: "Name", content: TITLE }],
        dimension: [
          {
            type: "Dimension",
            value: FIRST_VALUE,
            classified_as: [{ id: HEIGHT, type: "Type", _label: "Height" }],
            unit: { id: CENTIMETERS, type: "MeasurementUnit", _label: "centimeters" },
          },
        ],
      },
    },
  });
  expect(response.status()).toBe(201);
  const created = (await response.json()) as { id: string };
  await context.close();
  return created.id;
}

async function openEditorPage(browser: Browser, id: string): Promise<Page> {
  const page = await (await browser.newContext()).newPage();
  await openEditor(page, id);
  return page;
}

const form = (page: Page) => page.getByTestId("profile-form");
const title = (page: Page) => form(page).getByLabel("Title 1", { exact: true });
const material = (page: Page, name: string) =>
  form(page).getByRole("group", { name: "Materials" }).getByRole("button", { name, exact: true });
const dimensionValue = (page: Page) =>
  form(page).getByLabel("Value of Dimensions 1", { exact: true });

/** Run `step(1)`, `step(2)`, … until `deadline`; return how many steps ran. */
async function until(deadline: number, step: (n: number) => Promise<void>): Promise<number> {
  let n = 0;
  while (Date.now() < deadline) {
    n += 1;
    await step(n);
  }
  return n;
}

/** Click a toggle pill and wait until this editor shows the new state. */
async function toggle(pill: Locator): Promise<void> {
  const pressed = (await pill.getAttribute("aria-pressed")) === "true";
  await pill.click();
  await expect(pill).toHaveAttribute("aria-pressed", String(!pressed));
}

test("three editors co-edit one record for a minute and the publish keeps every edit", async ({
  browser,
}) => {
  test.setTimeout(SESSION_MS + 180_000);
  const id = await createRecord(browser);
  const a = await openEditorPage(browser, id);
  const b = await openEditorPage(browser, id);
  const c = await openEditorPage(browser, id);
  const pages = [a, b, c];
  for (const page of pages) {
    await expect(title(page)).toHaveValue(TITLE);
    await expect(dimensionValue(page)).toHaveValue(String(FIRST_VALUE));
    await expect(material(page, "canvas")).toHaveAttribute("aria-pressed", "false");
    await expect(material(page, "oil paint")).toHaveAttribute("aria-pressed", "false");
  }

  // The session: the three loops run at the same time until the deadline.
  const deadline = Date.now() + SESSION_MS;
  const words: string[] = [];
  let value = FIRST_VALUE;
  let oilPaintToggles = 0;
  const [titleEdits, canvasToggles, valueEdits] = await Promise.all([
    // A: append a word to the title, typed key by key at the end.
    until(deadline, async (n) => {
      const word = ` a${String(n)}`;
      await title(a).evaluate((element) => {
        const field = element as HTMLTextAreaElement;
        field.focus();
        field.setSelectionRange(field.value.length, field.value.length);
      });
      await title(a).pressSequentially(word);
      words.push(word);
      await a.waitForTimeout(250);
    }),
    // B: switch canvas on and off.
    until(deadline, async () => {
      await toggle(material(b, "canvas"));
      await b.waitForTimeout(400);
    }),
    // C: type a new dimension value; every third time, also switch oil paint.
    until(deadline, async (n) => {
      value = FIRST_VALUE + n;
      await dimensionValue(c).fill(String(value));
      if (n % 3 === 0) {
        await toggle(material(c, "oil paint"));
        oilPaintToggles += 1;
      }
      await c.waitForTimeout(300);
    }),
  ]);
  const session = { titleEdits, canvasToggles, valueEdits, oilPaintToggles };
  test.info().annotations.push({ type: "collab-session", description: JSON.stringify(session) });
  console.log(`collab session: ${JSON.stringify(session)}`);
  expect(titleEdits).toBeGreaterThan(10);
  expect(canvasToggles).toBeGreaterThan(10);
  expect(valueEdits).toBeGreaterThan(10);

  // Every editor converges on the same draft: all of A's words, B's and C's last states.
  const expectedTitle = TITLE + words.join("");
  const canvasOn = canvasToggles % 2 === 1;
  const oilPaintOn = oilPaintToggles % 2 === 1;
  for (const page of pages) {
    await expect(title(page)).toHaveValue(expectedTitle);
    await expect(dimensionValue(page)).toHaveValue(String(value));
    await expect(material(page, "canvas")).toHaveAttribute("aria-pressed", String(canvasOn));
    await expect(material(page, "oil paint")).toHaveAttribute("aria-pressed", String(oilPaintOn));
  }
  await expectNoAxeViolations(a);

  // A publishes; the public JSON-LD has every edit.
  await publishDraft(a, "Three editors", 1);
  const response = await a.request.get(`/id/${id}`, {
    headers: { Accept: "application/ld+json" },
  });
  expect(response.status()).toBe(200);
  const body = await response.text();
  const published = JSON.parse(body) as Published;
  expect(published.identified_by.map((name) => name.content)).toEqual([expectedTitle]);
  expect(published.dimension).toHaveLength(1);
  expect(published.dimension[0]?.value).toBe(value);
  expect(published.dimension[0]?.unit.id).toBe(CENTIMETERS);
  expect(published.dimension[0]?.classified_as.map((type) => type.id)).toEqual([HEIGHT]);
  const materials = (published.made_of ?? []).map((reference) => reference.id).sort();
  expect(materials).toEqual(
    [...(canvasOn ? [CANVAS] : []), ...(oilPaintOn ? [OIL_PAINT] : [])].sort(),
  );

  // The corpus round-trip invariant holds for the published record.
  const verdict = execFileSync(
    "uv",
    ["run", "--project", BACKEND, "--locked", "python", ROUNDTRIP],
    { input: body, encoding: "utf8" },
  );
  expect(JSON.parse(verdict)).toEqual({ ok: true });

  for (const page of pages) {
    await page.context().close();
  }
});
