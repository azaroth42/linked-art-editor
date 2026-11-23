import { expect, test, type Locator, type Page } from "@playwright/test";

import { expectNoAxeViolations } from "./axe.ts";
import {
  IMAGE_ID,
  IMAGE_REGION,
  IMAGE_SERVICE,
  IMAGE_SIZE,
  KYLIX_ID,
  MANIFEST,
  MANIFEST_CANVAS,
  MANIFEST_ID,
  PAINTER_ID,
} from "./readerRecords.ts";

// The Public Reader (Step 1.21) is served by `lae api` at /id/{uuid}, through the preview
// server's proxy. It must work without JavaScript and must never load the SPA (decision D8).

/** A 1×1 PNG, the answer to every image request of the test image service. */
const PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
  "base64",
);

/** Whether a request is for something the SPA's build or dev server serves. */
function isSpaAsset(url: string): boolean {
  const { pathname } = new URL(url);
  if (pathname.startsWith("/reader/assets/")) {
    return false;
  }
  return (
    pathname.startsWith("/assets/") ||
    pathname.startsWith("/src/") ||
    pathname.startsWith("/@") ||
    pathname.startsWith("/locales/") ||
    pathname.endsWith(".tsx") ||
    pathname === "/index.html"
  );
}

/** Collect the URL of every request the page makes from now on. */
function requests(page: Page): string[] {
  const urls: string[] = [];
  page.on("request", (request) => urls.push(request.url()));
  return urls;
}

/** A one-Canvas Manifest painted by the test image service. */
function manifest(): object {
  return {
    "@context": "http://iiif.io/api/presentation/3/context.json",
    id: MANIFEST,
    type: "Manifest",
    label: { en: ["Kylix"] },
    items: [
      {
        id: MANIFEST_CANVAS,
        type: "Canvas",
        ...IMAGE_SIZE,
        items: [
          {
            id: `${MANIFEST_CANVAS}/page`,
            type: "AnnotationPage",
            items: [
              {
                id: `${MANIFEST_CANVAS}/page/image`,
                type: "Annotation",
                motivation: "painting",
                target: MANIFEST_CANVAS,
                body: {
                  id: `${IMAGE_SERVICE}/full/max/0/default.jpg`,
                  type: "Image",
                  format: "image/jpeg",
                  ...IMAGE_SIZE,
                  service: [{ id: IMAGE_SERVICE, type: "ImageService3", profile: "level0" }],
                },
              },
            ],
          },
        ],
      },
    ],
  };
}

/** Answer the test Manifest and image service locally: no request leaves the machine. */
async function serveImage(page: Page): Promise<void> {
  const cors = { "Access-Control-Allow-Origin": "*" };
  await page.route("https://iiif.test/**", (route) => {
    if (route.request().url() === MANIFEST) {
      return route.fulfill({ headers: cors, json: manifest() });
    }
    if (route.request().url() === `${IMAGE_SERVICE}/info.json`) {
      return route.fulfill({
        headers: cors,
        json: {
          "@context": "http://iiif.io/api/image/3/context.json",
          id: IMAGE_SERVICE,
          type: "ImageService3",
          protocol: "http://iiif.io/api/image",
          profile: "level0",
          ...IMAGE_SIZE,
          sizes: [{ width: 300, height: 225 }, IMAGE_SIZE],
        },
      });
    }
    return route.fulfill({ headers: cors, body: PIXEL, contentType: "image/png" });
  });
}

interface Position {
  x: number;
  y: number;
  width: number;
  height: number;
}

async function position(panel: Locator): Promise<Position> {
  return panel.evaluate((element) =>
    (element as HTMLElement & { getPosition(): Position }).getPosition(),
  );
}

/** The view once Canvas Panel's animation has stopped (two reads 250 ms apart agree). */
async function settled(page: Page, panel: Locator): Promise<Position> {
  let last = await position(panel);
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.waitForTimeout(250);
    const current = await position(panel);
    if (JSON.stringify(current) === JSON.stringify(last)) {
      return current;
    }
    last = current;
  }
  throw new Error("the viewer did not settle");
}

/** The view shows the whole region, and is zoomed in on it (2 px for rounding). */
function framesRegion(view: Position): boolean {
  const { x, y, width, height } = IMAGE_REGION;
  return (
    view.x <= x + 2 &&
    view.y <= y + 2 &&
    view.x + view.width >= x + width - 2 &&
    view.y + view.height >= y + height - 2 &&
    view.width <= IMAGE_SIZE.width * 0.75
  );
}

test("/id/:uuid renders the record with no script and has no axe violations", async ({ page }) => {
  const urls = requests(page);
  const response = await page.goto(`/id/${KYLIX_ID}`);
  expect(response?.headers()["content-type"]).toBe("text/html; charset=utf-8");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.locator("html")).toHaveAttribute("dir", "ltr");
  await expect(page).toHaveTitle("Red-figure kylix – Laertes");
  await expect(page.getByRole("heading", { level: 1, name: "Red-figure kylix" })).toBeVisible();
  for (const heading of ["Names", "Identifiers", "Production", "Materials", "Dimensions"]) {
    await expect(page.getByRole("heading", { level: 2, name: heading })).toBeVisible();
  }
  // The painter is another local record: a link to its reader page, labelled from entity_labels.
  await expect(page.getByRole("link", { name: "Douris" })).toHaveAttribute(
    "href",
    `/id/${PAINTER_ID}`,
  );
  await expect(page.getByText(/^480 BC\s–\s470 BC$/)).toBeVisible();
  await expect(page.getByText("31.5 centimeters")).toBeVisible();
  await expect(page.getByRole("link", { name: "JSON-LD" })).toHaveAttribute(
    "href",
    "?format=jsonld",
  );
  await expect(page.getByRole("link", { name: "Turtle" })).toHaveAttribute("href", "?format=ttl");
  // No session cookie, so no edit link.
  await expect(page.getByRole("link", { name: "Edit this record" })).toHaveCount(0);
  await expectNoAxeViolations(page);
  expect(urls.filter(isSpaAsset)).toEqual([]);
  expect(urls.filter((url) => url.endsWith(".js"))).toEqual([]);
});

test("the reader links to the serializations and to related records", async ({ page }) => {
  await page.goto(`/id/${KYLIX_ID}`);
  const [jsonld] = await Promise.all([
    page.waitForResponse((response) => response.url().endsWith("?format=jsonld")),
    page.getByRole("link", { name: "JSON-LD" }).click(),
  ]);
  expect(jsonld.headers()["content-type"]).toBe(
    'application/ld+json;profile="https://linked.art/ns/v1/linked-art.json"',
  );
  await page.goto(`/id/${KYLIX_ID}`);
  await page.getByRole("link", { name: "Douris" }).click();
  await expect(page).toHaveURL(`/id/${PAINTER_ID}`);
  await expect(page.getByRole("heading", { level: 1, name: "Douris" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Birth" })).toBeVisible();
  await expectNoAxeViolations(page);
});

test("?version=N shows the version banner and has no axe violations", async ({ page }) => {
  await page.goto(`/id/${KYLIX_ID}?version=1`);
  await expect(page.getByText("You are viewing version 1 of this record")).toBeVisible();
  await expect(page.getByText("This is the current version.")).toBeVisible();
  await expect(page.getByRole("link", { name: "JSON-LD" })).toHaveAttribute(
    "href",
    "?version=1&format=jsonld",
  );
  await expectNoAxeViolations(page);
});

test("a IIIF image opens in Canvas Panel, at its region, and pans and zooms from the keyboard", async ({
  page,
}) => {
  await serveImage(page);
  const urls = requests(page);
  await page.goto(`/id/${IMAGE_ID}`);
  const panel = page.locator("canvas-panel");
  await expect(panel).toHaveAttribute("canvas-id", `${IMAGE_SERVICE}/reader-canvas`);
  // world-ready hides the image shown without JavaScript.
  await expect(page.locator("img.fallback")).toBeHidden();
  await expect(page.getByText(/use the arrow keys to pan/)).toBeVisible();
  await expect.poll(async () => framesRegion(await position(panel))).toBe(true);

  // Keyboard: the skip link, then the viewer.
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(panel).toBeFocused();
  // 0 resets to the whole image; = and - zoom from there (Canvas Panel's keyboard zoom steps
  // from its own zoom level, not from the region a Content State framed); arrows pan.
  await page.keyboard.press("0");
  const home = await settled(page, panel);
  expect(home.width).toBeGreaterThanOrEqual(IMAGE_SIZE.width);
  await page.keyboard.press("=");
  const zoomed = await settled(page, panel);
  expect(zoomed.width).toBeLessThan(home.width);
  await page.keyboard.press("ArrowRight");
  expect((await settled(page, panel)).x).toBeGreaterThan(zoomed.x);
  await page.keyboard.press("-");
  expect((await settled(page, panel)).width).toBeGreaterThan(zoomed.width);

  await expectNoAxeViolations(page);
  expect(urls.filter(isSpaAsset)).toEqual([]);
  const scripts = urls.filter((url) => url.endsWith(".js")).map((url) => new URL(url).pathname);
  expect(scripts[0]).toBe("/reader/assets/reader.js");
  expect(scripts.every((path) => path.startsWith("/reader/assets/"))).toBe(true);
  expect(scripts.length).toBeGreaterThan(1); // the Canvas Panel chunk, loaded on demand
});

test("a IIIF Manifest opens at its first Canvas and has no axe violations", async ({ page }) => {
  await serveImage(page);
  const urls = requests(page);
  await page.goto(`/id/${MANIFEST_ID}`);
  const panel = page.locator("canvas-panel");
  await expect(panel).toHaveAttribute("manifest-id", MANIFEST);
  await expect(panel).toHaveAttribute("canvas-id", MANIFEST_CANVAS);
  // Without an image of its own, the page links to the Manifest until the viewer is ready.
  await expect(page.getByRole("link", { name: "Open the IIIF manifest" })).toBeHidden();
  await expect.poll(async () => (await position(panel)).width).toBeGreaterThan(0);
  await expectNoAxeViolations(page);
  expect(urls.filter(isSpaAsset)).toEqual([]);
});

test("first contentful paint without JavaScript is under one second", async ({
  browser,
}, testInfo) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  try {
    await page.goto(`/id/${KYLIX_ID}`);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    const paint = await page.evaluate(
      () => performance.getEntriesByName("first-contentful-paint")[0]?.startTime ?? -1,
    );
    testInfo.annotations.push({
      type: "first-contentful-paint",
      description: `${String(paint)} ms`,
    });
    expect(paint).toBeGreaterThan(0);
    expect(paint).toBeLessThan(1000);
  } finally {
    await context.close();
  }
});
