import { afterEach, describe, expect, it, vi } from "vitest";

import type { CanvasPanelElement, Vault } from "./canvasPanel.ts";
import {
  clampRegion,
  encodeContentState,
  imageServiceManifest,
  parseRegion,
  regionContentState,
  setUpViewer,
} from "./viewer.ts";

const SERVICE = "https://iiif.test/image/kylix";
const MANIFEST = "https://iiif.test/manifest.json";
const CANVAS = "https://iiif.test/canvas/1";

function decode(encoded: string): unknown {
  const base64 = encoded.replace(/-/g, "+").replace(/_/g, "/");
  return JSON.parse(decodeURIComponent(atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "="))));
}

describe("parseRegion", () => {
  it("reads pixel and percent regions", () => {
    expect(parseRegion("10,20,30,40")).toEqual({ unit: "px", x: 10, y: 20, width: 30, height: 40 });
    expect(parseRegion("pct:10,20.5,30,40")).toEqual({
      unit: "pct",
      x: 10,
      y: 20.5,
      width: 30,
      height: 40,
    });
  });

  it.each([null, undefined, "", "1,2,3", "a,b,c,d", "1,2,3,4,5"])("rejects %j", (value) => {
    expect(parseRegion(value)).toBeNull();
  });
});

describe("clampRegion", () => {
  it("rounds to whole pixels", () => {
    expect(
      clampRegion({ unit: "px", x: 10.4, y: 20.6, width: 30.2, height: 40 }, 100, 100),
    ).toEqual({ x: 10, y: 21, width: 31, height: 40 });
  });

  it("clamps to the canvas and never goes negative", () => {
    expect(clampRegion({ unit: "px", x: -50, y: 80, width: 100, height: 100 }, 100, 100)).toEqual({
      x: 0,
      y: 80,
      width: 50,
      height: 20,
    });
  });

  it("scales percentages to the canvas", () => {
    expect(clampRegion({ unit: "pct", x: 10, y: 50, width: 50, height: 50 }, 1200, 900)).toEqual({
      x: 120,
      y: 450,
      width: 600,
      height: 450,
    });
  });

  it("returns null when nothing of the region is on the canvas", () => {
    expect(clampRegion({ unit: "px", x: 200, y: 0, width: 10, height: 10 }, 100, 100)).toBeNull();
    expect(clampRegion({ unit: "px", x: 0, y: 0, width: 0, height: 10 }, 100, 100)).toBeNull();
  });
});

describe("regionContentState", () => {
  it("targets the region of the canvas, part of the manifest, encoded per Content State API", () => {
    const encoded = regionContentState(MANIFEST, CANVAS, { x: 1, y: 2, width: 3, height: 4 });
    expect(encoded).not.toMatch(/[+/=]/);
    expect(decode(encoded)).toMatchObject({
      type: "Annotation",
      motivation: ["contentState"],
      target: {
        id: `${CANVAS}#xywh=1,2,3,4`,
        type: "Canvas",
        partOf: [{ id: MANIFEST, type: "Manifest" }],
      },
    });
  });

  it("encodes non-ASCII text", () => {
    expect(decode(encodeContentState(JSON.stringify({ label: "Scène" })))).toEqual({
      label: "Scène",
    });
  });
});

describe("imageServiceManifest", () => {
  it("paints one canvas of the service's size with the service", () => {
    const info = {
      "@context": "http://iiif.io/api/image/3/context.json",
      width: 1200,
      height: 900,
    };
    const { manifestId, canvasId, manifest } = imageServiceManifest(SERVICE, info);
    const canvas = (manifest.items as Record<string, unknown>[])[0];
    expect(manifest).toMatchObject({ id: manifestId, type: "Manifest" });
    expect(canvas).toMatchObject({ id: canvasId, type: "Canvas", width: 1200, height: 900 });
    expect(JSON.stringify(canvas)).toContain('"type":"ImageService3"');
    expect(JSON.stringify(canvas)).toContain(`"id":"${SERVICE}"`);
  });

  it("recognises an Image API 2 service", () => {
    const info = { "@context": "http://iiif.io/api/image/2/context.json", width: 10, height: 10 };
    expect(JSON.stringify(imageServiceManifest(SERVICE, info).manifest)).toContain(
      '"type":"ImageService2"',
    );
  });
});

describe("setUpViewer", () => {
  afterEach(() => {
    document.body.innerHTML = "";
  });

  function render(attributes: string): {
    panel: CanvasPanelElement;
    fallback: HTMLElement;
    help: HTMLElement;
  } {
    document.body.innerHTML = `<figure><canvas-panel ${attributes} aria-describedby="help"></canvas-panel><p id="help"></p><img class="fallback" alt=""></figure>`;
    return {
      panel: document.querySelector("canvas-panel") as CanvasPanelElement,
      fallback: document.querySelector(".fallback") as HTMLElement,
      help: document.getElementById("help") as HTMLElement,
    };
  }

  function fakeVault(manifestItems: string[] = [CANVAS]): Vault {
    return {
      loadManifest: vi.fn((id: string) =>
        Promise.resolve({ id, items: manifestItems.map((item) => ({ id: item, type: "Canvas" })) }),
      ),
      get: vi.fn((reference: { id: string }) => ({ id: reference.id, width: 1000, height: 800 })),
    };
  }

  it("shows a manifest's first canvas and applies the region after world-ready", async () => {
    const { panel, fallback } = render(`manifest-id="${MANIFEST}" data-region="-10,10,500,2000"`);
    const setContentStateFromText = vi.fn();
    panel.setContentStateFromText = setContentStateFromText;
    await setUpViewer(panel, fakeVault([CANVAS, "https://iiif.test/canvas/2"]));
    expect(panel.getAttribute("canvas-id")).toBe(CANVAS);
    expect(fallback.hidden).toBe(false);
    expect(setContentStateFromText).not.toHaveBeenCalled();

    panel.dispatchEvent(new CustomEvent("world-ready"));
    expect(fallback.hidden).toBe(true);
    expect(setContentStateFromText).toHaveBeenCalledOnce();
    const [encoded, immediate] = setContentStateFromText.mock.calls[0] as [string, boolean];
    expect(immediate).toBe(true);
    expect(decode(encoded)).toMatchObject({ target: { id: `${CANVAS}#xywh=0,10,490,790` } });
  });

  it("builds a manifest from an image service's info.json", async () => {
    const { panel } = render(`data-image-service="${SERVICE}"`);
    const vault = fakeVault();
    const load = vi.fn(() => Promise.resolve({ width: 1200, height: 900 }));
    await setUpViewer(panel, vault, load);
    expect(load).toHaveBeenCalledWith(`${SERVICE}/info.json`);
    expect(vault.loadManifest).toHaveBeenCalledWith(
      `${SERVICE}/reader-manifest`,
      expect.anything(),
    );
    expect(panel.getAttribute("manifest-id")).toBe(`${SERVICE}/reader-manifest`);
    expect(panel.getAttribute("canvas-id")).toBe(`${SERVICE}/reader-canvas`);
  });

  it("hides the panel and keeps the fallback when the service cannot be read", async () => {
    const { panel, fallback, help } = render(`data-image-service="${SERVICE}"`);
    await setUpViewer(panel, fakeVault(), () => Promise.reject(new Error("offline")));
    expect(panel.hidden).toBe(true);
    expect(help.hidden).toBe(true);
    expect(fallback.hidden).toBe(false);
    expect(panel.hasAttribute("canvas-id")).toBe(false);
  });

  it("hides the panel when the manifest has no canvas", async () => {
    const { panel } = render(`manifest-id="${MANIFEST}"`);
    await setUpViewer(panel, fakeVault([]));
    expect(panel.hidden).toBe(true);
  });
});
