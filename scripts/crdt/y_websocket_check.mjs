// Wire check of `lae crdt-ws` with the browser's own libraries: Yjs + y-websocket
// (`WebsocketProvider`), loaded from frontend/node_modules and run in Node (global WebSocket).
//
// Used by backend/tests/crdt/test_y_websocket.py, which starts the server, plays a pycrdt peer,
// and reads the JSON this script prints. It can also be run by hand against `uv run lae crdt-ws`:
//
//   node scripts/crdt/y_websocket_check.mjs --url ws://127.0.0.1:9521/ws/rooms \
//     --record <uuid> --epoch <n> --token <PAT with edit_draft> --viewer-token <PAT of a Viewer> \
//     --viewer-id <uuid of that Viewer> --label "<records.data._label>"
//
// What it checks (each is a boolean in `checks`; the process exits 1 if any is false):
//   hydrated          the server's document arrives through sync (the root map `record`)
//   peer_edit         a change made by the pycrdt peer arrives live
//   peer_presence     the pycrdt peer's awareness state arrives, stamped by the server
//   server_state      the server's awareness state {kind: "server", draft_epoch, read_only}
//   own_presence_...  what this client publishes comes back stamped with its real identity
//   viewer_synced     a Viewer receives the document, including this client's edits
//   viewer_flagged    the Viewer's user id is in the server state's `read_only`
//   viewer_dropped    a change made by the Viewer does not reach the editor
//   stale_closed      a provider with another epoch is closed with 4409 `stale-epoch`
//   stale_terminal    ... and y-websocket does not reconnect after it

import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const here = path.dirname(fileURLToPath(import.meta.url));
const frontend = path.resolve(here, "../../frontend");
const require = createRequire(path.join(frontend, "package.json"));
const Y = await import(pathToFileURL(require.resolve("yjs")).href);
const { WebsocketProvider } = await import(pathToFileURL(require.resolve("y-websocket")).href);

const { values: args } = parseArgs({
  options: {
    url: { type: "string" },
    record: { type: "string" },
    epoch: { type: "string" },
    token: { type: "string" },
    "viewer-token": { type: "string" },
    "viewer-id": { type: "string" },
    label: { type: "string" },
    timeout: { type: "string", default: "10000" },
  },
});
const TIMEOUT = Number(args.timeout);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function until(what, predicate, timeout = TIMEOUT) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (predicate()) return true;
    await sleep(20);
  }
  failures.push(`timed out: ${what}`);
  return false;
}

function open(token, epoch) {
  const doc = new Y.Doc();
  const provider = new WebsocketProvider(args.url, args.record, doc, {
    params: { token, epoch: String(epoch) },
    disableBc: true,
  });
  return { doc, provider, record: doc.getMap("record") };
}

const states = (provider) => [...provider.awareness.getStates().values()];
const serverState = (provider) => states(provider).find((state) => state.kind === "server");

const checks = {};
const failures = [];
const details = { yjs: require("yjs/package.json").version };
details.y_websocket = require("y-websocket/package.json").version;
details.node = process.version;

// --- an editor -------------------------------------------------------------------------------
const editor = open(args.token, args.epoch);
checks.hydrated =
  (await until("editor synced", () => editor.provider.synced)) &&
  editor.record.get("_label") === args.label &&
  editor.record.get("type") === "HumanMadeObject";

editor.provider.awareness.setLocalState({ cursor: "js", user_id: "forged", kind: "agent" });
editor.record.set("js_marker", "from-yjs");
const statementKey = [...editor.record.keys()].find((key) => key.startsWith('["referred_to_by"'));
const statement = statementKey ? editor.record.get(statementKey).get("content") : null;
if (statement instanceof Y.Text) statement.insert(0, "JS:");
else failures.push("no Y.Text statement in the hydrated document");

checks.peer_edit = await until(
  "the pycrdt peer's edit",
  () => editor.record.get("py_marker") === "from-pycrdt",
);
checks.peer_presence = await until("the pycrdt peer's presence", () =>
  states(editor.provider).some((state) => state.cursor === "py" && state.kind === "human"),
);
checks.server_state = await until(
  "the server's awareness state",
  () => serverState(editor.provider)?.draft_epoch === Number(args.epoch),
);

// --- a Viewer (read-only) --------------------------------------------------------------------
const viewer = open(args["viewer-token"], args.epoch);
checks.viewer_synced =
  (await until("viewer synced", () => viewer.provider.synced)) &&
  viewer.record.get("js_marker") === "from-yjs" &&
  viewer.record.get("py_marker") === "from-pycrdt";
checks.viewer_flagged = await until("read_only lists the viewer", () =>
  (serverState(viewer.provider)?.read_only ?? []).includes(args["viewer-id"]),
);
// The editor's own state, as the server stamped it, seen from the other client.
checks.own_presence_stamped = await until("the editor's presence at the viewer", () =>
  states(viewer.provider).some(
    (state) => state.cursor === "js" && state.kind === "human" && state.user_id !== "forged",
  ),
);
viewer.record.set("viewer_marker", "must-not-arrive");
editor.record.set("js_marker_2", "after-viewer");
await until("the editor's later edit at the viewer", () => viewer.record.has("js_marker_2"));
await sleep(300);
checks.viewer_dropped = !editor.record.has("viewer_marker");

// --- a stale epoch ---------------------------------------------------------------------------
const stale = open(args.token, Number(args.epoch) + 1);
let closeEvent = null;
let closedEvents = 0;
let connecting = 0;
stale.provider.on("connection-close", (event) => (closeEvent ??= event));
stale.provider.on("closed", () => (closedEvents += 1));
stale.provider.on("status", ({ status }) => (connecting += status === "connecting" ? 1 : 0));
await until("stale provider closed", () => closeEvent !== null);
details.stale_close = closeEvent && { code: closeEvent.code, reason: closeEvent.reason };
checks.stale_closed = closeEvent?.code === 4409 && closeEvent?.reason === "stale-epoch";
const attempts = connecting;
await sleep(1500); // y-websocket's first retry would come after 100 ms
checks.stale_terminal = closedEvents === 1 && connecting === attempts && !stale.provider.synced;

details.editor_states = states(editor.provider);
for (const { provider, doc } of [editor, viewer, stale]) {
  provider.destroy();
  doc.destroy();
}
const ok = failures.length === 0 && Object.values(checks).every(Boolean);
console.log(JSON.stringify({ ok, checks, failures, details }, null, 2));
process.exit(ok ? 0 : 1);
