/**
 * Setting up one server-rendered `<canvas-panel>` on the Public Reader page (Step 1.21).
 *
 * The page (backend `lae/reader/templates/record.html`) renders the element with kebab-case
 * attributes, string booleans, a `height` attribute and `tabindex="0"`, plus either
 * `manifest-id` (a IIIF Manifest) or `data-image-service` (a IIIF Image API service), and maybe
 * `data-region` (`x,y,w,h` or `pct:x,y,w,h`). Canvas Panel shows nothing without a Canvas, so this
 * module picks one: the Manifest's first Canvas, or a one-Canvas Manifest made from the image
 * service's `info.json` and loaded into the vault. Once the viewer reports `world-ready`, the
 * page's fallback image is hidden and the region, rounded and clamped to whole pixels inside the
 * Canvas, is applied as a Content State with `setContentStateFromText` (it is ignored when given
 * on first load). These are the rules of docs/spikes/S5-canvas-panel.md, section 8.
 */
import type { CanvasPanelElement, Vault } from "./canvasPanel.ts";

/** A region of an image: pixels, or percentages of the image (IIIF Image API `pct:`). */
export interface Region {
  unit: "px" | "pct";
  x: number;
  y: number;
  width: number;
  height: number;
}

/** A region in whole Canvas pixels, as in a Media Fragment `#xywh=x,y,w,h`. */
export interface Box {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** The fields of an Image API 2 or 3 `info.json` the reader reads. */
export interface ImageInfo {
  width: number;
  height: number;
  [key: string]: unknown;
}

/** Parse a `data-region` value; `null` when it is absent or malformed. */
export function parseRegion(value: string | null | undefined): Region | null {
  if (!value) {
    return null;
  }
  const percent = value.startsWith("pct:");
  const numbers = (percent ? value.slice(4) : value).split(",").map(Number);
  if (numbers.length !== 4 || numbers.some((n) => !Number.isFinite(n))) {
    return null;
  }
  const [x = 0, y = 0, width = 0, height = 0] = numbers;
  return { unit: percent ? "pct" : "px", x, y, width, height };
}

function clamp(value: number, max: number): number {
  return Math.min(Math.max(value, 0), max);
}

/** Round and clamp a region to whole pixels inside a Canvas; `null` if nothing is left. */
export function clampRegion(region: Region, canvasWidth: number, canvasHeight: number): Box | null {
  const scaleX = region.unit === "pct" ? canvasWidth / 100 : 1;
  const scaleY = region.unit === "pct" ? canvasHeight / 100 : 1;
  const left = clamp(Math.round(region.x * scaleX), canvasWidth);
  const top = clamp(Math.round(region.y * scaleY), canvasHeight);
  const right = clamp(Math.round((region.x + region.width) * scaleX), canvasWidth);
  const bottom = clamp(Math.round((region.y + region.height) * scaleY), canvasHeight);
  if (right <= left || bottom <= top) {
    return null;
  }
  return { x: left, y: top, width: right - left, height: bottom - top };
}

/** Content State API 1.0 section 6: percent-encode as UTF-8, then base64url without padding. */
export function encodeContentState(json: string): string {
  return btoa(encodeURIComponent(json)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** The Media Fragment of a box, `xywh=x,y,w,h`. */
export function xywh({ x, y, width, height }: Box): string {
  return `xywh=${[x, y, width, height].join(",")}`;
}

/** An encoded Content State that targets `box` on one Canvas of one Manifest. */
export function regionContentState(manifestId: string, canvasId: string, box: Box): string {
  const target = `${canvasId}#${xywh(box)}`;
  return encodeContentState(
    JSON.stringify({
      "@context": "http://iiif.io/api/presentation/3/context.json",
      id: `${manifestId}/content-state`,
      type: "Annotation",
      motivation: ["contentState"],
      target: { id: target, type: "Canvas", partOf: [{ id: manifestId, type: "Manifest" }] },
    }),
  );
}

/** Whether an `info.json` describes an Image API 2 service (else it is read as version 3). */
function isImageApi2(info: ImageInfo): boolean {
  const context = info["@context"];
  const contexts: unknown[] = Array.isArray(context) ? context : [context];
  return contexts.some((value) => typeof value === "string" && value.includes("/image/2/"));
}

/**
 * A Presentation 3 Manifest with one Canvas painted by an image service. Its ids are made-up
 * paths below the service (never fetched: the Manifest is loaded into the vault directly) and
 * have no fragment, since a Content State adds `#xywh=` to the Canvas id.
 */
export function imageServiceManifest(
  service: string,
  info: ImageInfo,
): { manifestId: string; canvasId: string; manifest: Record<string, unknown> } {
  const manifestId = `${service}/reader-manifest`;
  const canvasId = `${service}/reader-canvas`;
  const description = {
    ...info,
    id: typeof info.id === "string" ? info.id : service,
    type:
      typeof info.type === "string"
        ? info.type
        : isImageApi2(info)
          ? "ImageService2"
          : "ImageService3",
  };
  const { width, height } = info;
  const manifest = {
    "@context": "http://iiif.io/api/presentation/3/context.json",
    id: manifestId,
    type: "Manifest",
    label: { none: [service] },
    items: [
      {
        id: canvasId,
        type: "Canvas",
        width,
        height,
        items: [
          {
            id: `${canvasId}/page`,
            type: "AnnotationPage",
            items: [
              {
                id: `${canvasId}/page/image`,
                type: "Annotation",
                motivation: "painting",
                target: canvasId,
                body: {
                  id: `${service}/full/max/0/default.jpg`,
                  type: "Image",
                  format: "image/jpeg",
                  width,
                  height,
                  service: [description],
                },
              },
            ],
          },
        ],
      },
    ],
  };
  return { manifestId, canvasId, manifest };
}

/** Fetch a JSON document, failing on any non-2xx status. */
export async function fetchJson(url: string): Promise<unknown> {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${String(response.status)} ${url}`);
  }
  return response.json();
}

function isImageInfo(value: unknown): value is ImageInfo {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const { width, height } = value as Record<string, unknown>;
  return typeof width === "number" && typeof height === "number" && width > 0 && height > 0;
}

/** The Canvas a panel shows, with the size the region is clamped to. */
export interface ShownCanvas {
  manifestId: string;
  canvasId: string;
  width: number;
  height: number;
}

/** Find the Canvas to show: a Manifest's first, or one made from an image service. */
export async function resolveCanvas(
  panel: CanvasPanelElement,
  vault: Vault,
  load: (url: string) => Promise<unknown> = fetchJson,
): Promise<ShownCanvas | null> {
  const service = panel.dataset.imageService;
  if (service) {
    const info = await load(`${service}/info.json`);
    if (!isImageInfo(info)) {
      return null;
    }
    const { manifestId, canvasId, manifest } = imageServiceManifest(service, info);
    await vault.loadManifest(manifestId, manifest);
    return { manifestId, canvasId, width: info.width, height: info.height };
  }
  const manifestId = panel.getAttribute("manifest-id");
  if (!manifestId) {
    return null;
  }
  const manifest = await vault.loadManifest(manifestId);
  const first = manifest?.items[0];
  if (!first) {
    return null;
  }
  const canvas = vault.get({ id: first.id, type: "Canvas" });
  return {
    manifestId,
    canvasId: first.id,
    width: canvas?.width ?? 0,
    height: canvas?.height ?? 0,
  };
}

/**
 * Point a server-rendered `<canvas-panel>` at a Canvas, hide the fallback image once the viewer
 * is ready, and apply the region. If there is nothing to show, hide the panel and its help text
 * and keep the fallback.
 */
export async function setUpViewer(
  panel: CanvasPanelElement,
  vault: Vault,
  load: (url: string) => Promise<unknown> = fetchJson,
): Promise<void> {
  const figure = panel.closest("figure") ?? panel.parentElement;
  const fallbacks = figure ? [...figure.querySelectorAll<HTMLElement>(".fallback")] : [];
  let shown: ShownCanvas | null;
  try {
    shown = await resolveCanvas(panel, vault, load);
  } catch {
    shown = null;
  }
  if (shown === null) {
    panel.hidden = true;
    const help = panel.getAttribute("aria-describedby");
    if (help) {
      document.getElementById(help)?.setAttribute("hidden", "");
    }
    return;
  }
  const { manifestId, canvasId, width, height } = shown;
  const region = parseRegion(panel.dataset.region);
  panel.addEventListener(
    "world-ready",
    () => {
      for (const fallback of fallbacks) {
        fallback.hidden = true;
      }
      const box = region && width > 0 && height > 0 ? clampRegion(region, width, height) : null;
      if (box) {
        panel.setContentStateFromText?.(regionContentState(manifestId, canvasId, box), true);
      }
    },
    { once: true },
  );
  if (panel.getAttribute("manifest-id") !== manifestId) {
    panel.setAttribute("manifest-id", manifestId);
  }
  panel.setAttribute("canvas-id", canvasId);
}
