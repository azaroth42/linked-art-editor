/**
 * The parts of Canvas Panel 1.0.74 and its IIIF vault that the Public Reader script uses.
 *
 * Canvas Panel assigns its methods to the element when it connects, so every member is optional
 * (docs/spikes/S5-canvas-panel.md, section 5). The vault is the one Canvas Panel creates on
 * import and keeps on `globalThis.IIIF_VAULT`; every panel on the page shares it.
 */

/** A `<canvas-panel>` element once the bundle has defined it. */
export type CanvasPanelElement = HTMLElement & {
  setContentStateFromText?: (text: string, immediate?: boolean) => void;
};

/** A Canvas as the vault stores it after normalizing a Manifest. */
export interface VaultCanvas {
  id: string;
  width?: number;
  height?: number;
}

/** A Manifest as the vault stores it: its Canvases are references. */
export interface VaultManifest {
  id: string;
  items: { id: string; type: string }[];
}

/** The subset of `@iiif/vault` the reader uses. */
export interface Vault {
  /** Fetch a Manifest (or take `json` instead of fetching) and normalize it into the store. */
  loadManifest(id: string, json?: unknown): Promise<VaultManifest | undefined>;
  get(reference: { id: string; type: "Canvas" }): VaultCanvas | null;
}

/** The global vault Canvas Panel creates when its bundle is imported. */
export function globalVault(): Vault | undefined {
  return (globalThis as { IIIF_VAULT?: Vault }).IIIF_VAULT;
}
