/**
 * The editor's live connection to a record's draft (Phase 1, Step 1.17; ADR-0027).
 *
 * A {@link RecordSession} owns one `Y.Doc` for one **draft epoch** of one record: the
 * `WebsocketProvider` to `/ws/rooms/{id}?epoch=<n>` (`crdt-ws`; the epoch is mandatory and the
 * server closes any other with `4409 stale-epoch`), the presence `Awareness`, a `Y.UndoManager`
 * that tracks only this client's own transactions, and a `y-indexeddb` copy of the document.
 *
 * - A document never moves to another epoch: a new epoch means a new session with an empty
 *   `Y.Doc`, so two hydrations are never merged (R2). The IndexedDB store is named after the
 *   record **and** the epoch for the same reason; a stale session's edits stay in its store.
 * - `BroadcastChannel` sync between tabs is off: it bypasses the server and its epoch guard.
 * - Close codes 4400–4499 are final (y-websocket stops); `closeCode` reports them. `1011` and
 *   `1012` make y-websocket reconnect with the same epoch.
 * - Local edits are made with {@link RecordSession.localOrigin} (pass it as the `origin` of the
 *   `paths.ts` operations, or use `transact`); undo never reverts a co-editor's change.
 */
import { useEffect, useState, useSyncExternalStore } from "react";
import { IndexeddbPersistence } from "y-indexeddb";
import { Awareness } from "y-protocols/awareness";
import { WebsocketProvider } from "y-websocket";
import * as Y from "yjs";

import { recordMap } from "./project.ts";

/** Close code: the draft was published, discarded or reverted (another epoch is current). */
export const CLOSE_STALE_EPOCH = 4409;

/** Who this client is, published as presence (the server stamps the authoritative values). */
export interface RecordDocUser {
  id: string;
  displayName: string;
}

/** Another client in the room. */
export interface Peer {
  clientId: number;
  userId: string | null;
  displayName: string | null;
  kind: string | null;
  readOnly: boolean;
}

/** What a session reports; a new object whenever anything in it changes. */
export interface RecordSessionState {
  connected: boolean;
  synced: boolean;
  /** The final close code (4400–4499) when the server ended the session, else `null`. */
  closeCode: number | null;
  peers: Peer[];
  /** The server lists this user as read-only (no `edit_draft`): edits are dropped. */
  readOnly: boolean;
  canUndo: boolean;
  canRedo: boolean;
}

export interface RecordSessionOptions {
  recordId: string;
  epoch: number;
  user: RecordDocUser;
  /** WebSocket base URL of the rooms; default {@link roomsUrl}. */
  serverUrl?: string;
  /** Keep the document in IndexedDB (default: when the browser has IndexedDB). */
  persist?: boolean;
  /** Tests: a WebSocket implementation. */
  WebSocketPolyfill?: typeof WebSocket;
}

/** `ws(s)://<host>/ws/rooms` of the page's origin (the Vite proxy sends `/ws/` to `crdt-ws`). */
export function roomsUrl(location: Pick<Location, "protocol" | "host"> = globalThis.location) {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/rooms`;
}

/** The IndexedDB store of one draft epoch of one record. */
export function draftStoreName(recordId: string, epoch: number): string {
  return `lae-draft:${recordId}:${String(epoch)}`;
}

/** An undo manager over the record that tracks only transactions made with `localOrigin`. */
export function createScopedUndoManager(doc: Y.Doc, localOrigin: object): Y.UndoManager {
  return new Y.UndoManager(recordMap(doc), { trackedOrigins: new Set([localOrigin]) });
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** Every other client's presence; the server's own state (`kind: "server"`) is not a peer. */
export function presencePeers(awareness: Awareness): Peer[] {
  const peers: Peer[] = [];
  for (const [clientId, state] of awareness.getStates()) {
    if (clientId === awareness.clientID || state.kind === "server") {
      continue;
    }
    peers.push({
      clientId,
      userId: text(state.user_id),
      displayName: text(state.display_name),
      kind: text(state.kind),
      readOnly: state.read_only === true,
    });
  }
  return peers.sort((a, b) => a.clientId - b.clientId);
}

function serverReadOnly(awareness: Awareness, userId: string): boolean {
  for (const state of awareness.getStates().values()) {
    if (state.kind === "server" && Array.isArray(state.read_only)) {
      return (state.read_only as unknown[]).includes(userId);
    }
  }
  return false;
}

/** One record, one epoch: the document and everything attached to it. */
export class RecordSession {
  readonly doc: Y.Doc;
  readonly awareness: Awareness;
  readonly provider: WebsocketProvider;
  readonly persistence: IndexeddbPersistence | null;
  readonly undoManager: Y.UndoManager;
  /** Origin of this client's own edits; the only origin the undo manager tracks. */
  readonly localOrigin: object;
  private state: RecordSessionState = {
    connected: false,
    synced: false,
    closeCode: null,
    peers: [],
    readOnly: false,
    canUndo: false,
    canRedo: false,
  };
  private readonly listeners = new Set<() => void>();
  private readonly userId: string;

  constructor(options: RecordSessionOptions) {
    this.userId = options.user.id;
    this.doc = new Y.Doc();
    this.localOrigin = Object.freeze({ recordSession: options.recordId });
    const persist = options.persist ?? typeof indexedDB !== "undefined";
    this.persistence = persist
      ? new IndexeddbPersistence(draftStoreName(options.recordId, options.epoch), this.doc)
      : null;
    this.awareness = new Awareness(this.doc);
    this.awareness.setLocalState({
      user_id: options.user.id,
      display_name: options.user.displayName,
      kind: "human",
    });
    this.undoManager = createScopedUndoManager(this.doc, this.localOrigin);
    this.provider = new WebsocketProvider(
      options.serverUrl ?? roomsUrl(),
      options.recordId,
      this.doc,
      {
        awareness: this.awareness,
        params: { epoch: String(options.epoch) },
        disableBc: true,
        ...(options.WebSocketPolyfill ? { WebSocketPolyfill: options.WebSocketPolyfill } : {}),
      },
    );
    this.provider.on("status", ({ status }) => this.update({ connected: status === "connected" }));
    this.provider.on("sync", (synced: boolean) => this.update({ synced }));
    this.provider.on("closed", ({ code }) => this.update({ closeCode: code }));
    this.awareness.on("change", this.onPresence);
    for (const event of ["stack-item-added", "stack-item-popped", "stack-cleared"] as const) {
      this.undoManager.on(event, this.onUndoStack);
    }
  }

  private readonly onPresence = () => {
    this.update({
      peers: presencePeers(this.awareness),
      readOnly: serverReadOnly(this.awareness, this.userId),
    });
  };

  private readonly onUndoStack = () => {
    this.update({ canUndo: this.undoManager.canUndo(), canRedo: this.undoManager.canRedo() });
  };

  private update(change: Partial<RecordSessionState>): void {
    const changed = (Object.keys(change) as (keyof RecordSessionState)[]).some(
      (key) => JSON.stringify(change[key]) !== JSON.stringify(this.state[key]),
    );
    if (changed) {
      this.state = { ...this.state, ...change };
      for (const listener of this.listeners) {
        listener();
      }
    }
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  readonly getState = (): RecordSessionState => this.state;

  /** Run `fn` as one transaction of this client (tracked by undo). */
  transact<T>(fn: () => T): T {
    return this.doc.transact(fn, this.localOrigin);
  }

  readonly undo = (): void => {
    this.undoManager.undo();
  };

  readonly redo = (): void => {
    this.undoManager.redo();
  };

  destroy(): void {
    this.listeners.clear();
    this.awareness.off("change", this.onPresence);
    this.undoManager.destroy();
    this.provider.destroy();
    this.awareness.destroy();
    void this.persistence?.destroy();
    this.doc.destroy();
  }
}

export interface UseRecordDocOptions extends Omit<RecordSessionOptions, "epoch" | "user"> {
  /** The draft epoch from `POST /api/records` or `GET /api/records/{id}`; `null` = not yet. */
  epoch: number | null;
  user: RecordDocUser | null;
}

export interface RecordDoc extends RecordSessionState {
  doc: Y.Doc | null;
  provider: WebsocketProvider | null;
  session: RecordSession | null;
  undo: () => void;
  redo: () => void;
}

const CLOSED: RecordSessionState = {
  connected: false,
  synced: false,
  closeCode: null,
  peers: [],
  readOnly: false,
  canUndo: false,
  canRedo: false,
};
const noSubscribe = () => () => undefined;
const closedState = () => CLOSED;
const nothing = () => undefined;

/**
 * Open the record's draft for editing: `{doc, provider, undo, redo, connected, peers}` and the
 * rest of {@link RecordSessionState}. Nothing connects until `epoch` and `user` are known; a
 * change of record or epoch closes the session and opens a new one with a new document.
 */
export function useRecordDoc(options: UseRecordDocOptions): RecordDoc {
  const { recordId, epoch, user, serverUrl, persist, WebSocketPolyfill } = options;
  const userId = user?.id ?? null;
  const displayName = user?.displayName ?? null;
  const [session, setSession] = useState<RecordSession | null>(null);

  useEffect(() => {
    if (epoch === null || userId === null || displayName === null) {
      return undefined;
    }
    const opened = new RecordSession({
      recordId,
      epoch,
      user: { id: userId, displayName },
      serverUrl,
      persist,
      WebSocketPolyfill,
    });
    setSession(opened);
    return () => {
      setSession(null);
      opened.destroy();
    };
  }, [recordId, epoch, userId, displayName, serverUrl, persist, WebSocketPolyfill]);

  const state = useSyncExternalStore(
    session?.subscribe ?? noSubscribe,
    session?.getState ?? closedState,
  );
  return {
    ...state,
    doc: session?.doc ?? null,
    provider: session?.provider ?? null,
    session,
    undo: session?.undo ?? nothing,
    redo: session?.redo ?? nothing,
  };
}
