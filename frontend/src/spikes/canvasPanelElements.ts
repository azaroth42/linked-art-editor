/**
 * Spike S5: TypeScript surface for the Canvas Panel web components under React 19.
 *
 * `@digirati/canvas-panel-web-components` ships Preact-based typings that do not describe the
 * custom elements as DOM elements, so the editor declares the subset it uses here. Attribute
 * names are kebab-case on purpose: React 19 sets a prop as a DOM property only when
 * `propName in element`, and Canvas Panel defines its properties under the attribute names
 * (`"manifest-id"`), so camelCase props (`manifestId`) silently become unobserved attributes.
 */
import type { DetailedHTMLProps, HTMLAttributes } from "react";

/** A region in canvas coordinates, as in a Media Fragment `#xywh=x,y,w,h`. */
export interface XywhRegion {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** What `enableContentStateSelection` passes back after the user draws a box. */
export interface ContentStateSelectionEvent {
  contentState: {
    id: string;
    type: "Canvas";
    partOf: { id: string; type: "Manifest" }[];
  };
  encodedContentState: string;
  selection: { type: "BoxSelector"; spatial: XywhRegion };
}

/** Transport actions Canvas Panel attaches to the element when it renders a Sound or Video. */
export interface CanvasPanelMediaActions {
  play(): void;
  pause(): void;
  playPause(): void;
  mute(): void;
  unmute(): void;
  setTime(seconds: number): void;
}

/**
 * Public API that Canvas Panel 1.0.74 assigns to the element instance once it has connected
 * and rendered. Every member is optional because none exists before `connectedCallback`.
 */
export interface CanvasPanelApi {
  getCanvasId(): string | undefined;
  getManifestId(): string | undefined;
  getPosition(): Partial<XywhRegion>;
  setCanvas(canvasId: string): void;
  goToTarget(target: XywhRegion, options?: { immediate?: boolean; padding?: number }): void;
  getTarget(): { selector?: { type: string; spatial?: XywhRegion } } | undefined;
  enableContentStateSelection(callback: (event: ContentStateSelectionEvent) => void): void;
  disableContentStateSelection(): void;
  getContentState(): { contentState: { id: string }; encodedContentState: string };
  setContentStateFromText(text: string, immediate?: boolean): void;
  mediaActions: CanvasPanelMediaActions;
  mediaElement: { current: HTMLMediaElement | null };
}

export type CanvasPanelElement = HTMLElement & Partial<CanvasPanelApi>;

/** The `react-iiif-vault` SimpleViewer context `<sequence-panel>` exposes as `element.sequence`. */
export interface SequencePanelSequence {
  items: { id: string }[];
  sequence: number[][];
  currentSequenceIndex: number;
  nextCanvas(): void;
  previousCanvas(): void;
  setCurrentCanvasId(canvasId: string): void;
}

export type SequencePanelElement = HTMLElement & {
  sequence?: SequencePanelSequence;
  getManifestId?(): string | undefined;
};

/** Detail of `world-ready` and `zoom`. */
export interface CanvasPanelZoomDetail {
  scaleFactor: number;
  worldWidth: number;
  worldHeight: number;
  canZoomIn: boolean;
  canZoomOut: boolean;
}

/** Canvas Panel parses booleans with `value.toLowerCase() === "true"`, so pass strings. */
type BooleanString = "true" | "false";

type ElementProps<E extends HTMLElement> = DetailedHTMLProps<HTMLAttributes<E>, E>;

/** Attributes shared by every Atlas-based Canvas Panel element. */
interface AtlasElementAttributes {
  region?: string;
  target?: string;
  highlight?: string;
  height?: string;
  width?: string;
  "atlas-mode"?: "explore" | "sketch";
  "move-events"?: BooleanString;
  "granular-move-events"?: BooleanString;
  "preferred-formats"?: string;
  "a11y-title"?: string;
  "a11y-role"?: string;
  responsive?: BooleanString;
  interactive?: BooleanString;
  /** React 19 binds `on<event>` props on custom elements with `addEventListener("<event>")`. */
  "onworld-ready"?: (event: CustomEvent<CanvasPanelZoomDetail>) => void;
  onzoom?: (event: CustomEvent<CanvasPanelZoomDetail>) => void;
  onmove?: (event: CustomEvent<XywhRegion>) => void;
}

export interface CanvasPanelAttributes extends AtlasElementAttributes {
  "manifest-id"?: string;
  "canvas-id"?: string;
  "iiif-content"?: string;
  "oncanvas-change"?: (event: CustomEvent<{ canvas?: string }>) => void;
  "onmedia-displayed"?: (event: Event) => void;
}

export interface SequencePanelAttributes extends AtlasElementAttributes {
  "manifest-id"?: string;
  "start-canvas"?: string;
  "paging-enabled"?: BooleanString;
  onsequence?: (event: CustomEvent<SequencePanelSequence>) => void;
  "onsequence-change"?: (event: CustomEvent<{ index: number; total: number }>) => void;
}

declare module "react" {
  // eslint-disable-next-line @typescript-eslint/no-namespace -- augmenting React's JSX namespace is how custom elements are typed.
  namespace JSX {
    interface IntrinsicElements {
      "canvas-panel": ElementProps<CanvasPanelElement> & CanvasPanelAttributes;
      "sequence-panel": ElementProps<SequencePanelElement> & SequencePanelAttributes;
    }
  }
}
