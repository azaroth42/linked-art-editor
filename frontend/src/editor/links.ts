/** Where the editor's pages link to, and what one passes to the next. */

/** The public page of a record (`/id/{uuid}`, the server-rendered reader), or of one version. */
export function publicRecordPath(id: string, version?: number): string {
  const path = `/id/${encodeURIComponent(id)}`;
  return version === undefined ? path : `${path}?version=${String(version)}`;
}

/** The record editor. */
export function editorPath(id: string): string {
  return `/edit/${encodeURIComponent(id)}`;
}

/** The Revision History Browser. */
export function historyPath(id: string): string {
  return `/records/${encodeURIComponent(id)}/history`;
}

/** What the History page tells the editor it opens after a revert (navigation state). */
export interface RevertedTo {
  /** The version the draft now holds. */
  version: number;
  /** The epoch of that draft; the notice is about this draft only. */
  draftEpoch: number;
}

/** The navigation state of `/edit/:id` after a revert. */
export function revertedState(reverted: RevertedTo): { reverted: RevertedTo } {
  return { reverted };
}

/** The {@link RevertedTo} in a location's state, if the page was opened by a revert. */
export function revertedFrom(state: unknown): RevertedTo | null {
  if (typeof state !== "object" || state === null || !("reverted" in state)) {
    return null;
  }
  const reverted: unknown = state.reverted;
  if (typeof reverted !== "object" || reverted === null) {
    return null;
  }
  const { version, draftEpoch } = reverted as Record<string, unknown>;
  return typeof version === "number" && typeof draftEpoch === "number"
    ? { version, draftEpoch }
    : null;
}
