/**
 * Spike S5: Canvas Panel in React 19, proved against headless Chromium.
 *
 * Run from `frontend/` with `corepack pnpm test:spike-s5`. The tests load public IIIF Cookbook
 * resources, so they need the network. Tests wrapped in `test.fail()` record measured gaps: they
 * assert the capability the editor wants and are expected to fail against Canvas Panel 1.0.74.
 * If one starts passing, Playwright reports it, and `docs/spikes/S5-canvas-panel.md` needs updating.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import type {
  CanvasPanelElement,
  SequencePanelElement,
  XywhRegion,
} from "./canvasPanelElements.ts";
import {
  API_REGION,
  AUDIO,
  BOOK,
  CONTENT_STATE_REGION,
  IMAGE,
  IMAGE_REGION,
  VIDEO,
  decodeContentState,
} from "./fixtures.ts";

const PAGE = "/src/spikes/canvas-panel.html";
const IMAGE_SERVICE =
  "https://iiif.io/api/image/3.0/example/reference/918ecd18c2592080851777620de9bcb5-gottingen";

async function openSpike(page: Page): Promise<void> {
  await page.goto(PAGE);
  await expect(page.getByTestId("image-ready")).toHaveText("true");
}

function position(panel: Locator): Promise<Partial<XywhRegion> | undefined> {
  return panel.evaluate((el: CanvasPanelElement) => el.getPosition?.());
}

/** The viewport once pan and zoom transitions have finished (unchanged for 250 ms). */
async function settledPosition(panel: Locator): Promise<Partial<XywhRegion> | undefined> {
  let previous = await position(panel);
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await panel.page().waitForTimeout(250);
    const current = await position(panel);
    if (JSON.stringify(current) === JSON.stringify(previous)) {
      return current;
    }
    previous = current;
  }
  throw new Error("Canvas Panel viewport did not settle");
}

/** True when the viewport shows all of `region` and not much more (it keeps its aspect ratio). */
function framesRegion(viewport: Partial<XywhRegion> | undefined, region: XywhRegion): boolean {
  if (!viewport || viewport.x === undefined || viewport.y === undefined) {
    return false;
  }
  const { x, y, width = 0, height = 0 } = viewport;
  const tolerance = 2;
  const contains =
    x <= region.x + tolerance &&
    y <= region.y + tolerance &&
    x + width >= region.x + region.width - tolerance &&
    y + height >= region.y + region.height - tolerance;
  const tight = width <= region.width * 1.5 && height <= region.height * 1.5;
  return contains && tight;
}

function shadowMedia(panel: Locator): Locator {
  // Playwright CSS locators pierce open shadow roots; Canvas Panel paints media as <video>.
  return panel.locator("video");
}

async function expectPlayback(page: Page, panelId: string, playButton: string): Promise<void> {
  const panel = page.getByTestId(panelId);
  const media = shadowMedia(panel);
  const decoder = await media.evaluate((el: HTMLMediaElement) =>
    el.canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
  );
  test.skip(
    decoder === "",
    "This Chromium build has no H.264/AAC decoder; rerun with SPIKE_S5_CHROME=1 (branded Chrome).",
  );
  await expect
    .poll(() => media.evaluate((el: HTMLMediaElement) => el.readyState))
    .toBeGreaterThanOrEqual(1);
  await page.getByRole("button", { name: playButton }).click();
  await expect
    .poll(() => media.evaluate((el: HTMLMediaElement) => el.currentTime), { timeout: 30_000 })
    .toBeGreaterThan(0.5);
  expect(await media.evaluate((el: HTMLMediaElement) => el.error)).toBeNull();
}

test.describe("Canvas Panel 1.0.74 web components in React 19", () => {
  test("loads an image Canvas by manifest id and canvas id", async ({ page }) => {
    const tiles: string[] = [];
    page.on("response", (response) => {
      const type = response.headers()["content-type"] ?? "";
      if (response.url().startsWith(IMAGE_SERVICE) && type.startsWith("image/")) {
        tiles.push(response.url());
      }
    });

    await openSpike(page);
    const panel = page.getByTestId("cp-image");

    // `canvas-change` and `world-ready` reach React 19 through `on<event>` props.
    await expect(page.getByTestId("image-canvas")).toHaveText(IMAGE.canvas);
    await expect(panel).toHaveAttribute("manifest-id", IMAGE.manifest);
    await expect(panel).toHaveAttribute("canvas-id", IMAGE.canvas);
    await expect(panel.locator("canvas")).toHaveCount(1);
    await expect.poll(() => tiles.length).toBeGreaterThan(0);

    // The whole Canvas (4032 x 3024) is framed at the home position.
    const home = await position(panel);
    expect(framesRegion(home, { x: 0, y: 0, width: 4032, height: 3024 })).toBe(true);
  });

  test("sets a region through the region attribute and through goToTarget", async ({ page }) => {
    await openSpike(page);
    const panel = page.getByTestId("cp-image");

    await page.getByRole("button", { name: "Set region attribute" }).click();
    // React 19 assigned the string to the element's `region` property, which reflects it.
    await expect(panel).toHaveAttribute("region", "1000,800,1200,900");
    await expect.poll(async () => framesRegion(await position(panel), IMAGE_REGION)).toBe(true);
    const target = await panel.evaluate((el: CanvasPanelElement) => el.getTarget?.());
    expect(target?.selector?.type).toBe("BoxSelector");
    expect(target?.selector?.spatial).toMatchObject({ ...IMAGE_REGION });

    await page.getByRole("button", { name: "Go to region (API)" }).click();
    await expect.poll(async () => framesRegion(await position(panel), API_REGION)).toBe(true);
  });

  test("reads a drawn selection back as an xywh Content State", async ({ page }) => {
    await openSpike(page);
    const panel = page.getByTestId("cp-image");
    const selectionId = page.getByTestId("selection-id");
    const from = { x: 0.25, y: 0.25 };
    const to = { x: 0.6, y: 0.7 };

    await page.getByRole("button", { name: "Draw a region" }).click();
    // Sketch mode attaches its mouse listeners in a Preact effect after the next paint, and a
    // drag that lands first pans instead. Let effects run, then read a settled viewport before
    // each drag, and drag again until a box is drawn.
    await page.evaluate(
      () =>
        new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 100))),
        ),
    );
    const drawn: {
      viewport?: Partial<XywhRegion>;
      box?: { x: number; y: number; width: number; height: number } | null;
    } = {};
    await expect(async () => {
      drawn.viewport = await settledPosition(panel);
      const box = await panel.boundingBox();
      drawn.box = box;
      if (!box) {
        throw new Error("Canvas Panel has no layout box");
      }
      await page.mouse.move(box.x + box.width * from.x, box.y + box.height * from.y);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width * to.x, box.y + box.height * to.y, { steps: 10 });
      await page.mouse.up();
      await expect(selectionId).toHaveText(
        new RegExp(`^${IMAGE.canvas}#xywh=\\d+,\\d+,\\d+,\\d+$`),
        { timeout: 1_000 },
      );
    }).toPass({ timeout: 20_000 });
    const { viewport, box } = drawn;
    if (!viewport?.width || viewport.x === undefined || viewport.y === undefined || !box) {
      throw new Error("Canvas Panel did not report its viewport");
    }
    const match = /#xywh=(\d+),(\d+),(\d+),(\d+)$/.exec((await selectionId.textContent()) ?? "");
    if (!match) {
      throw new Error("Selection is not an xywh fragment");
    }
    const [x, y, width, height] = match.slice(1).map(Number) as [number, number, number, number];

    // Screen pixels to canvas pixels: one scale, since the viewer preserves the aspect ratio.
    const scale = viewport.width / box.width;
    const expected = {
      x: viewport.x + box.width * from.x * scale,
      y: viewport.y + box.height * from.y * scale,
      width: box.width * (to.x - from.x) * scale,
      height: box.height * (to.y - from.y) * scale,
    };
    const tolerance = 2 * scale + 1;
    expect(Math.abs(x - expected.x)).toBeLessThanOrEqual(tolerance);
    expect(Math.abs(y - expected.y)).toBeLessThanOrEqual(tolerance);
    expect(Math.abs(width - expected.width)).toBeLessThanOrEqual(tolerance);
    expect(Math.abs(height - expected.height)).toBeLessThanOrEqual(tolerance);

    // The same selection comes back as an encoded Content State (Content State API 1.0).
    const encoded = (await page.getByTestId("selection-encoded").textContent()) ?? "";
    expect(JSON.parse(decodeContentState(encoded))).toEqual({
      id: `${IMAGE.canvas}#xywh=${x},${y},${width},${height}`,
      type: "Canvas",
      partOf: [{ id: IMAGE.manifest, type: "Manifest" }],
    });
  });

  test("loads the Canvas and Manifest named by an iiif-content Content State", async ({ page }) => {
    await openSpike(page);
    const panel = page.getByTestId("cp-content-state");
    await expect(page.getByTestId("content-state-canvas")).toHaveText(BOOK.canvases[1]);
    await expect(page.getByTestId("content-state-ready")).toHaveText("true");
    expect(await panel.evaluate((el: CanvasPanelElement) => el.getManifestId?.())).toBe(
      BOOK.manifest,
    );
    expect(await panel.evaluate((el: CanvasPanelElement) => el.getCanvasId?.())).toBe(
      BOOK.canvases[1],
    );
  });

  test("applies a Content State region once the viewer is ready (setContentStateFromText)", async ({
    page,
  }) => {
    await openSpike(page);
    const panel = page.getByTestId("cp-content-state");
    await expect(page.getByTestId("content-state-ready")).toHaveText("true");
    const encoded = (await page.getByTestId("content-state-value").textContent()) ?? "";
    await panel.evaluate((el: CanvasPanelElement, value) => {
      el.setContentStateFromText?.(value, true);
    }, encoded);
    await expect
      .poll(async () => framesRegion(await position(panel), CONTENT_STATE_REGION))
      .toBe(true);
  });

  test("navigates a paged Manifest with sequence-panel", async ({ page }) => {
    await openSpike(page);
    const panel = page.getByTestId("sequence");
    const output = page.getByTestId("sequence-position");

    // Recipe 0009 is `behavior: paged`: five Canvases in three views (cover, then spreads).
    await expect(output).toHaveText("1 / 3");
    const sequence = await panel.evaluate((el: SequencePanelElement) => ({
      items: el.sequence?.items.map((item) => item.id),
      views: el.sequence?.sequence,
    }));
    expect(sequence.items).toEqual(BOOK.canvases);
    expect(sequence.views).toEqual([[0], [1, 2], [3, 4]]);

    await page.getByRole("button", { name: "Next" }).click();
    await expect(output).toHaveText("2 / 3");
    await page.getByRole("button", { name: "Next" }).click();
    await expect(output).toHaveText("3 / 3");
    await page.getByRole("button", { name: "Previous" }).click();
    await expect(output).toHaveText("2 / 3");
  });

  test("pans and zooms from the keyboard once focused", async ({ page }) => {
    await openSpike(page);
    const panel = page.getByTestId("cp-image");
    const before = await position(panel);
    await panel.focus();
    await page.keyboard.press("=");
    await expect.poll(async () => (await position(panel))?.width).toBeLessThan(before?.width ?? 0);
    const zoomed = await position(panel);
    await page.keyboard.press("ArrowRight");
    await expect.poll(async () => (await position(panel))?.x).toBeGreaterThan(zoomed?.x ?? 0);

    // The view the keyboard reached exports as a Content State: the keyboard path to a region.
    await page.waitForTimeout(600); // let the 500 ms pan transition settle
    const view = await position(panel);
    const exported = await panel.evaluate(
      (el: CanvasPanelElement) => el.getContentState?.().contentState.id,
    );
    // Measured: the values are the raw viewport, fractional and possibly negative (see gaps).
    expect(exported).toBe(
      `${IMAGE.canvas}#xywh=${view?.x},${view?.y},${view?.width},${view?.height}`,
    );
  });

  test("renders and plays the Video Canvas", async ({ page }) => {
    await openSpike(page);
    const panel = page.getByTestId("cp-video");
    // `media-displayed` reached React; the <video> is in Canvas Panel's shadow root.
    await expect(page.getByTestId("video-state")).not.toBeEmpty();
    await expect(shadowMedia(panel)).toHaveAttribute("src", VIDEO.media);
    // Canvas Panel draws no transport controls; the host builds them on `mediaActions`.
    expect(await shadowMedia(panel).evaluate((el: HTMLMediaElement) => el.controls)).toBe(false);
    await expectPlayback(page, "cp-video", "Play video");
  });

  test("renders and plays the Sound Canvas", async ({ page }) => {
    await openSpike(page);
    const panel = page.getByTestId("cp-audio");
    await expect(page.getByTestId("audio-state")).not.toBeEmpty();
    await expect(shadowMedia(panel)).toHaveAttribute("src", AUDIO.media);
    expect(await shadowMedia(panel).evaluate((el: HTMLMediaElement) => el.controls)).toBe(false);
    await expectPlayback(page, "cp-audio", "Play audio");
  });

  test("React 19 does not map camelCase props onto Canvas Panel attributes", async ({ page }) => {
    await openSpike(page);
    const panel = page.getByTestId("cp-camelcase");
    // HTML lower-cases the attribute names React falls back to, and Canvas Panel observes
    // only the kebab-case names, so nothing loads.
    await expect(panel).toHaveAttribute("manifestid", IMAGE.manifest);
    await expect(panel).not.toHaveAttribute("manifest-id");
    await expect(panel.locator("canvas")).toHaveCount(0);
  });

  test.describe("measured gaps (expected to fail)", () => {
    test("applies a Content State region on first load", async ({ page }) => {
      test.fail(true, "iiif-content sets the Canvas but ignores its xywh until the viewer exists");
      await openSpike(page);
      const panel = page.getByTestId("cp-content-state");
      await expect(page.getByTestId("content-state-ready")).toHaveText("true");
      await expect
        .poll(async () => framesRegion(await position(panel), CONTENT_STATE_REGION), {
          timeout: 5_000,
        })
        .toBe(true);
    });

    test("getCanvasId reports a canvas set by attribute", async ({ page }) => {
      test.fail(true, "getCanvasId()/getManifestId() stay undefined until the value changes");
      await openSpike(page);
      const panel = page.getByTestId("cp-image");
      expect(await panel.evaluate((el: CanvasPanelElement) => el.getCanvasId?.())).toBe(
        IMAGE.canvas,
      );
    });

    test("exports the current view as a valid xywh fragment", async ({ page }) => {
      test.fail(true, "getContentState() copies the raw viewport: fractional, negative at home");
      await openSpike(page);
      const exported = await page
        .getByTestId("cp-image")
        .evaluate((el: CanvasPanelElement) => el.getContentState?.().contentState.id);
      // Media Fragments 1.0: pixel xywh values are non-negative integers.
      expect(exported).toMatch(/#xywh=\d+,\d+,\d+,\d+$/);
    });

    test("seeks a Video to a temporal Content State (#t=)", async ({ page }) => {
      test.fail(true, "Canvas Panel handles only BoxSelector targets in Content State");
      await openSpike(page);
      const currentTime = await page.evaluate(
        async ({ manifest, canvas }) => {
          const panel = document.createElement("canvas-panel") as CanvasPanelElement;
          panel.setAttribute(
            "iiif-content",
            JSON.stringify({
              type: "Annotation",
              motivation: ["contentState"],
              target: {
                id: `${canvas}#t=30,60`,
                type: "Canvas",
                partOf: [{ id: manifest, type: "Manifest" }],
              },
            }),
          );
          panel.setAttribute("height", "200");
          document.body.append(panel);
          for (let i = 0; i < 80 && !panel.mediaElement?.current; i += 1) {
            await new Promise((resolve) => setTimeout(resolve, 250));
          }
          const media = panel.mediaElement?.current;
          if (!media) {
            return -1;
          }
          if (media.readyState < 1) {
            await new Promise((resolve) =>
              media.addEventListener("loadedmetadata", resolve, { once: true }),
            );
          }
          await new Promise((resolve) => setTimeout(resolve, 1_000));
          return media.currentTime;
        },
        { manifest: VIDEO.manifest, canvas: VIDEO.canvas },
      );
      expect(currentTime).toBeGreaterThanOrEqual(30);
    });
  });
});
