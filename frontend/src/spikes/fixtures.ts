/**
 * Spike S5: public IIIF Presentation 3 resources the spike page loads, all from the IIIF
 * Cookbook (https://iiif.io/api/cookbook/), which serves them with `Access-Control-Allow-Origin: *`.
 */
import type { XywhRegion } from "./canvasPanelElements.ts";

const COOKBOOK = "https://iiif.io/api/cookbook/recipe";

/** Recipe 0005: one Canvas painted with a IIIF Image API 3 service (4032 x 3024). */
export const IMAGE = {
  manifest: `${COOKBOOK}/0005-image-service/manifest.json`,
  canvas: `${COOKBOOK}/0005-image-service/canvas/p1`,
} as const;

/** Recipe 0009: a five-Canvas book, each Canvas backed by an image service. */
export const BOOK = {
  manifest: `${COOKBOOK}/0009-book-1/manifest.json`,
  canvases: [
    `${COOKBOOK}/0009-book-1/canvas/p1`,
    `${COOKBOOK}/0009-book-1/canvas/p2`,
    `${COOKBOOK}/0009-book-1/canvas/p3`,
    `${COOKBOOK}/0009-book-1/canvas/p4`,
    `${COOKBOOK}/0009-book-1/canvas/p5`,
  ],
} as const;

/** Recipe 0003: one Canvas painted with an MP4 Video (572 s). */
export const VIDEO = {
  manifest: `${COOKBOOK}/0003-mvm-video/manifest.json`,
  canvas: `${COOKBOOK}/0003-mvm-video/canvas`,
  media:
    "https://fixtures.iiif.io/video/indiana/lunchroom_manners/high/lunchroom_manners_1024kb.mp4",
} as const;

/** Recipe 0002: one Canvas painted with an MP4 Sound (1985 s). */
export const AUDIO = {
  manifest: `${COOKBOOK}/0002-mvm-audio/manifest.json`,
  canvas: `${COOKBOOK}/0002-mvm-audio/canvas`,
  media: "https://fixtures.iiif.io/audio/indiana/mahler-symphony-3/CD1/medium/128Kbps.mp4",
} as const;

/** The region the spike sets through the `region` attribute on the image Canvas. */
export const IMAGE_REGION: XywhRegion = { x: 1000, y: 800, width: 1200, height: 900 };

/** The region the spike sets through the `goToTarget` method on the image Canvas. */
export const API_REGION: XywhRegion = { x: 2400, y: 1200, width: 800, height: 450 };

/** The region carried by the Content State loaded into the second panel (book page 2). */
export const CONTENT_STATE_REGION: XywhRegion = { x: 400, y: 600, width: 1600, height: 1200 };

/** Formats a region as the value of a Media Fragment `xywh=` parameter. */
export function toXywh({ x, y, width, height }: XywhRegion): string {
  return `${x},${y},${width},${height}`;
}

/** A IIIF Content State annotation targeting part of one Canvas of one Manifest. */
export function canvasRegionContentState(manifest: string, canvas: string, fragment: string) {
  return {
    "@context": "http://iiif.io/api/presentation/3/context.json",
    id: "urn:lae:spike:s5:content-state",
    type: "Annotation",
    motivation: ["contentState"],
    target: {
      id: `${canvas}#${fragment}`,
      type: "Canvas",
      partOf: [{ id: manifest, type: "Manifest" }],
    },
  };
}

/** Content State API 1.0 section 6: percent-encode as UTF-8, then base64url without padding. */
export function encodeContentState(json: string): string {
  return btoa(encodeURIComponent(json)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** The inverse of `encodeContentState`. */
export function decodeContentState(encoded: string): string {
  const base64 = encoded.replace(/-/g, "+").replace(/_/g, "/");
  return decodeURIComponent(atob(base64.padEnd(Math.ceil(base64.length / 4) * 4, "=")));
}
