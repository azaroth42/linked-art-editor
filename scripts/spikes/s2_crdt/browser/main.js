// Spike S2 browser client: Yjs + y-websocket against the pycrdt-websocket server.
// Query parameters: ?server=ws://127.0.0.1:9507/ws&room=s2-browser&name=browser
import * as Y from "yjs";
import { WebsocketProvider } from "y-websocket";

const params = new URLSearchParams(location.search);
const server = params.get("server") ?? "ws://127.0.0.1:9507/ws";
const room = params.get("room") ?? "s2-browser";
const name = params.get("name") ?? "browser";
const doc = new Y.Doc();
const provider = new WebsocketProvider(server, room, doc, { disableBc: true });
const fields = doc.getMap("fields");
const notes = doc.getText("notes");
const latencies = [];
fields.observe((event, txn) => {
  if (txn.local) return;
  for (const key of event.keysChanged) {
    const v = fields.get(key);
    if (v && typeof v === "object" && "t" in v)
      latencies.push({ key, seq: v.seq, ms: Date.now() - v.t });
  }
});
const presence = []; // awareness changes as seen by this browser: {event, name, seq, at, ms}
const names = new Map();
provider.awareness.on("change", ({ added, updated, removed }) => {
  const at = Date.now();
  for (const id of [...added, ...updated]) {
    const s = provider.awareness.getStates().get(id);
    if (s?.user?.name) names.set(id, s.user.name);
    presence.push({
      event: added.includes(id) ? "added" : "updated",
      name: s?.user?.name,
      seq: s?.seq,
      at,
      ms: s?.t ? at - s.t : null,
    });
  }
  for (const id of removed) presence.push({ event: "removed", name: names.get(id), at });
});
provider.awareness.setLocalStateField("user", { name, kind: "human" });
provider.on("status", ({ status }) => (document.getElementById("status").textContent = status));
const snapshot = () => ({
  stateVector: Object.fromEntries(
    [...Y.decodeStateVector(Y.encodeStateVector(doc))].map(([k, v]) => [String(k), v]),
  ),
  fields: fields.toJSON(),
  notes: notes.toString(),
  awareness: Object.fromEntries(provider.awareness.getStates()),
  clientID: doc.clientID,
});
window.s2 = { Y, doc, provider, fields, notes, latencies, presence, snapshot };
