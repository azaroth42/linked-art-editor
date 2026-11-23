import { afterEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import { hydrate } from "./hydrate.ts";
import { addReference, getPath, listEntries, setScalar, valueToJson } from "./paths.ts";
import { project } from "./project.ts";
import { syncDocs } from "./testing.ts";
import {
  CLOSE_STALE_EPOCH,
  createScopedUndoManager,
  draftStoreName,
  presencePeers,
  RecordSession,
  roomsUrl,
} from "./useRecordDoc.ts";

const URI = "https://example.org/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
const RECORD_ID = "0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";

function replicaOf(doc: Y.Doc): Y.Doc {
  const other = new Y.Doc();
  Y.applyUpdate(other, Y.encodeStateAsUpdate(doc));
  return other;
}

describe("scoped undo", () => {
  it("reverts only the local client's edits, never a co-editor's", () => {
    const a = hydrate({
      id: URI,
      type: "HumanMadeObject",
      identified_by: [{ type: "Name", content: "Night Scene" }],
    });
    const b = replicaOf(a);
    const originA = { client: "a" };
    const originB = { client: "b" };
    const undoA = createScopedUndoManager(a, originA);
    createScopedUndoManager(b, originB);

    setScalar(a, "identified_by.content", "Night Scene by Day", { origin: originA });
    undoA.stopCapturing();
    addReference(b, "classified_as", "http://vocab.getty.edu/aat/300033618", {
      type: "Type",
      origin: originB,
    });
    setScalar(b, "identified_by.content", "A Night Scene", { origin: originB });
    syncDocs([a, b]);
    expect(getPath(a, "identified_by.content").map(valueToJson)).toEqual(["A Night Scene by Day"]);

    // B's changes arrived through sync (another origin): A's undo stack holds only its own edit.
    expect(undoA.undoStack).toHaveLength(1);
    undoA.undo();
    syncDocs([a, b]);
    for (const doc of [a, b]) {
      expect(getPath(doc, "identified_by.content").map(valueToJson)).toEqual(["A Night Scene"]);
      expect(listEntries(doc, "classified_as")).toHaveLength(1);
    }
    expect(project(a)).toEqual(project(b));
    undoA.redo();
    syncDocs([a, b]);
    expect(getPath(b, "identified_by.content").map(valueToJson)).toEqual(["A Night Scene by Day"]);
  });

  it("does not track transactions without the local origin", () => {
    const doc = hydrate({ id: URI, type: "HumanMadeObject" });
    const undo = createScopedUndoManager(doc, {});
    setScalar(doc, "x_note", "untracked");
    expect(undo.canUndo()).toBe(false);
  });
});

/** A WebSocket that records what it was opened with and can be closed by the "server". */
class FakeSocket {
  static instances: FakeSocket[] = [];
  static readonly OPEN = 1;
  readonly OPEN = 1;
  readyState = 0;
  binaryType = "arraybuffer";
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number; reason: string }) => void) | null = null;
  onmessage: ((event: { data: ArrayBuffer }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  readonly url: string;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }

  send(): void {}

  close(): void {
    this.readyState = 3;
  }

  serverOpens(): void {
    this.readyState = 1;
    this.onopen?.();
  }

  serverCloses(code: number, reason: string): void {
    this.onclose?.({ code, reason });
  }
}

describe("RecordSession", () => {
  const sessions: RecordSession[] = [];
  afterEach(() => {
    sessions.splice(0).forEach((session) => session.destroy());
    FakeSocket.instances = [];
  });

  function open(epoch = 3): RecordSession {
    const session = new RecordSession({
      recordId: RECORD_ID,
      epoch,
      user: { id: "user-1", displayName: "Ada" },
      serverUrl: "ws://example.test/ws/rooms",
      persist: false,
      WebSocketPolyfill: FakeSocket as unknown as typeof WebSocket,
    });
    sessions.push(session);
    return session;
  }

  it("connects to the room with its epoch and publishes the user's presence", () => {
    const session = open();
    expect(FakeSocket.instances.map((s) => s.url)).toEqual([
      `ws://example.test/ws/rooms/${RECORD_ID}?epoch=3`,
    ]);
    expect(session.awareness.getLocalState()).toEqual({
      user_id: "user-1",
      display_name: "Ada",
      kind: "human",
    });
    FakeSocket.instances[0]?.serverOpens();
    expect(session.getState().connected).toBe(true);
  });

  it("reports 4409 as final and does not reconnect", async () => {
    const session = open();
    const socket = FakeSocket.instances[0] as FakeSocket;
    socket.serverOpens();
    socket.serverCloses(CLOSE_STALE_EPOCH, "stale-epoch");
    expect(session.getState()).toMatchObject({ connected: false, closeCode: CLOSE_STALE_EPOCH });
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("tracks local edits for undo and lists peers without the server's own state", () => {
    const session = open();
    session.transact(() => session.doc.getMap("record").set("x_note", "mine"));
    expect(session.getState().canUndo).toBe(true);
    session.undo();
    expect(session.doc.getMap("record").has("x_note")).toBe(false);
    expect(session.getState().canRedo).toBe(true);

    const remote = new Y.Doc();
    const states = session.awareness.states;
    states.set(remote.clientID, {
      user_id: "user-2",
      display_name: "Grace",
      kind: "human",
      read_only: false,
    });
    states.set(remote.clientID + 1, { kind: "server", draft_epoch: 3, read_only: ["user-1"] });
    expect(presencePeers(session.awareness)).toEqual([
      {
        clientId: remote.clientID,
        userId: "user-2",
        displayName: "Grace",
        kind: "human",
        readOnly: false,
      },
    ]);
  });
});

describe("addresses", () => {
  it("builds the rooms URL and the per-epoch IndexedDB store name", () => {
    expect(roomsUrl({ protocol: "https:", host: "lae.example" })).toBe(
      "wss://lae.example/ws/rooms",
    );
    expect(roomsUrl({ protocol: "http:", host: "127.0.0.1:4173" })).toBe(
      "ws://127.0.0.1:4173/ws/rooms",
    );
    expect(draftStoreName(RECORD_ID, 2)).not.toBe(draftStoreName(RECORD_ID, 3));
  });
});
