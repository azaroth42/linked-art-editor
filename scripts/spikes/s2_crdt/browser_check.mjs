// Spike S2 browser wire-compatibility check.
//
// Serves browser/ (Yjs + y-websocket, ~40 lines) with Vite, opens it in headless Chromium via
// Playwright, and drives a pycrdt peer (browser_peer.py) in the same room on the S2 server.
// Proves: initial sync both ways, concurrent live edits, awareness both ways, reconnect with
// offline edits, graceful and abrupt leave, and identical final state in browser, Python peer
// and server room.
//
// Uses the frontend's installed yjs, y-websocket, vite and playwright; nothing is added to
// frontend/src. Run from the repo root with the S2 server on port 9507:
//
//   node scripts/spikes/s2_crdt/browser_check.mjs --out /tmp/lae-i7/s2-browser.json
import { spawn } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import readline from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";
import { isDeepStrictEqual, parseArgs } from "node:util";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../../..");
const frontend = path.join(repo, "frontend");
const require = createRequire(path.join(frontend, "package.json"));
const { chromium } = require("playwright");
const { createServer } = await import(pathToFileURL(require.resolve("vite")).href);

const { values: opts } = parseArgs({
  options: {
    ws: { type: "string", default: "ws://127.0.0.1:9507/ws" },
    http: { type: "string", default: "http://127.0.0.1:9507" },
    port: { type: "string", default: "9007" },
    out: { type: "string" },
    "skip-abrupt": { type: "boolean", default: false },
  },
});
const room = `s2-browser-${Date.now()}`;
const result = { room, checks: {}, timings_ms: {} };
const check = (name, ok, detail) => {
  result.checks[name] = { ok: Boolean(ok), ...(detail === undefined ? {} : { detail }) };
  console.log(
    `${ok ? "PASS" : "FAIL"} ${name}${detail === undefined ? "" : " " + JSON.stringify(detail)}`,
  );
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const version = (pkg) =>
  JSON.parse(readFileSync(path.join(frontend, "node_modules", pkg, "package.json"), "utf8"))
    .version;

// ---- pycrdt peer over JSON lines --------------------------------------------------------------
const env = { ...process.env };
delete env.VIRTUAL_ENV;
const peer = spawn(
  "uv",
  [
    "run",
    "--project",
    path.join(repo, "backend"),
    "--group",
    "spike",
    "python",
    path.join(here, "browser_peer.py"),
    "--url",
    opts.ws,
    "--room",
    room,
    "--name",
    "py",
  ],
  { stdio: ["pipe", "pipe", "inherit"], env },
);
const replies = [];
const waiters = [];
readline.createInterface({ input: peer.stdout }).on("line", (line) => {
  const msg = JSON.parse(line);
  const w = waiters.shift();
  if (w) w(msg);
  else replies.push(msg);
});
const call = (cmd) =>
  new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`peer timeout on ${cmd.cmd}`)), 60000);
    waiters.push((msg) => {
      clearTimeout(timer);
      if (msg.error) reject(new Error(`peer ${cmd.cmd}: ${msg.error}`));
      else resolve(msg);
    });
    peer.stdin.write(JSON.stringify(cmd) + "\n");
  });

// Browser-side writer, same payload shapes as the pycrdt clients.
const browserWrite = async ({ start, n, every }) => {
  for (let seq = start; seq < start + n; seq++) {
    const t = Date.now();
    const online = window.s2.provider.wsconnected;
    window.s2.fields.set("browser", { seq, t, online, by: "browser" });
    const text = window.s2.notes.toString();
    const bounds = [0, ...[...text.matchAll(/\]/g)].map((m) => m.index + 1)];
    const pos = bounds[Math.floor(Math.random() * bounds.length)];
    window.s2.notes.insert(pos, `[browser.${seq}@${t.toFixed(1)}${online ? "o" : "l"}]`);
    await new Promise((r) => setTimeout(r, every));
  }
};

const pct = (xs, q) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  return Math.round(s[Math.min(s.length - 1, Math.round((q / 100) * (s.length - 1)))] * 100) / 100;
};
const summary = (xs) => ({ n: xs.length, p50: pct(xs, 50), p95: pct(xs, 95), max: pct(xs, 100) });

let vite;
let browser;
try {
  vite = await createServer({
    configFile: false,
    root: path.join(here, "browser"),
    cacheDir: "/tmp/lae-i7/vite-cache",
    logLevel: "warn",
    resolve: {
      alias: [
        { find: /^yjs$/, replacement: path.join(frontend, "node_modules/yjs") },
        { find: /^y-websocket$/, replacement: path.join(frontend, "node_modules/y-websocket") },
      ],
    },
    server: {
      host: "127.0.0.1",
      port: Number(opts.port),
      strictPort: true,
      fs: { allow: [here, frontend] },
    },
  });
  await vite.listen();

  // 1. The pycrdt peer joins first, writes, announces presence once, then stays idle.
  await call({ cmd: "connect" });
  await call({ cmd: "write", n: 3, every: 0.05 });

  // 2. The browser joins and must receive the peer's state through the y-sync handshake.
  browser = await chromium.launch();
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
  page.on("pageerror", (e) => consoleErrors.push(String(e)));
  const tJoin = Date.now();
  const url = `http://127.0.0.1:${opts.port}/?server=${encodeURIComponent(opts.ws)}&room=${room}&name=browser`;
  await page.goto(url);
  await page.waitForFunction(() => window.s2?.provider.synced, null, {
    timeout: 15000,
    polling: 10,
  });
  result.timings_ms.browser_join_to_synced = Date.now() - tJoin;
  const initial = await page.evaluate(() => window.s2.snapshot());
  check(
    "initial sync python->browser",
    initial.fields.py?.seq === 3 && (initial.notes.match(/\[py\./g) ?? []).length === 3,
    {
      py: initial.fields.py,
    },
  );

  // 3. Awareness: the peer sees the browser at once (y-websocket announces on open) ...
  const tPres = Date.now();
  let pres = await call({ cmd: "presence" });
  while (!pres.first_seen.browser && Date.now() - tPres < 10000) {
    await sleep(50);
    pres = await call({ cmd: "presence" });
  }
  check("awareness browser->python", Boolean(pres.first_seen.browser), {
    after_join_ms: pres.first_seen.browser ? Math.round(pres.first_seen.browser - tJoin) : null,
  });
  // ... but the browser only sees the idle peer when the peer renews its state (pycrdt-websocket
  // does not send existing awareness states to a joining client; renewal is every 15 s).
  let joinPresenceMs = null;
  try {
    await page.waitForFunction(
      () =>
        [...window.s2.provider.awareness.getStates().values()].some((s) => s.user?.name === "py"),
      null,
      { timeout: 25000, polling: 20 },
    );
    joinPresenceMs = Date.now() - tJoin;
  } catch {
    joinPresenceMs = null;
  }
  result.timings_ms.browser_sees_idle_peer_after_join = joinPresenceMs;
  check("awareness python->browser (idle peer, no re-announce)", joinPresenceMs !== null, {
    after_join_ms: joinPresenceMs,
  });

  // 4. Live awareness python -> browser: 10 announcements at 100 ms.
  for (let i = 0; i < 10; i++) {
    await call({ cmd: "announce" });
    await sleep(100);
  }
  await sleep(300);
  const awLat = (await page.evaluate(() => window.s2.presence))
    .filter((p) => p.name === "py" && p.event !== "removed" && p.ms !== null)
    .slice(-10)
    .map((p) => p.ms);
  result.awareness_python_to_browser_ms = summary(awLat);
  check(
    "awareness live python->browser",
    awLat.length === 10,
    result.awareness_python_to_browser_ms,
  );

  // 5. Concurrent live edits from both sides: 40 Y.Map sets + 40 Y.Text inserts each.
  await Promise.all([
    call({ cmd: "write", n: 40, every: 0.05 }),
    page.evaluate(browserWrite, { start: 1, n: 40, every: 50 }),
  ]);
  const pyWait = await call({ cmd: "wait_seq", key: "browser", seq: 40, timeout: 10 });
  await page.waitForFunction(() => window.s2.fields.get("py")?.seq >= 43, null, { timeout: 10000 });
  check("live edits browser->python", pyWait.ok);
  const browserLat = (await page.evaluate(() => window.s2.latencies))
    .filter((l) => l.key === "py")
    .map((l) => l.ms);
  result.latency_python_to_browser_ms = summary(browserLat.slice(-40));

  // 6. Reconnect: browser goes offline, both sides edit, browser reconnects.
  await page.evaluate(() => window.s2.provider.disconnect());
  await Promise.all([
    call({ cmd: "write", n: 5, every: 0.05 }),
    page.evaluate(browserWrite, { start: 41, n: 5, every: 50 }),
  ]);
  const tRe = Date.now();
  await page.evaluate(() => window.s2.provider.connect());
  await page.waitForFunction(() => window.s2.provider.synced, null, {
    timeout: 15000,
    polling: 10,
  });
  result.timings_ms.browser_reconnect_to_synced = Date.now() - tRe;
  const reWait = await call({ cmd: "wait_seq", key: "browser", seq: 45, timeout: 10 });
  await page.waitForFunction(() => window.s2.fields.get("py")?.seq >= 48, null, { timeout: 10000 });
  // Extra wait at the peer once the browser reports synced (0 = already there).
  result.timings_ms.python_extra_wait_for_browser_offline_edits = Math.round(
    reWait.waited_ms ?? -1,
  );
  check("reconnect: offline edits exchanged both ways", reWait.ok);

  // 7. Final state: browser == python peer == server room.
  await sleep(500);
  const b = await page.evaluate(() => window.s2.snapshot());
  const p = await call({ cmd: "snapshot" });
  const s = await (
    await fetch(`${opts.http}/stats?room=${encodeURIComponent("/ws/" + room)}&content=1`)
  ).json();
  const srv = s.room_detail[`/ws/${room}`];
  const pyFields = JSON.parse(JSON.stringify(p.fields));
  result.latency_browser_to_python_ms = p.live_latency_ms;
  result.python_reads_browser_value_types = p.value_types.browser;
  check(
    "state vectors equal (browser, python, server)",
    isDeepStrictEqual(b.stateVector, p.state_vector) &&
      isDeepStrictEqual(b.stateVector, srv.state_vector),
    { browser: b.stateVector },
  );
  check(
    "Y.Map content equal (browser, python, server)",
    isDeepStrictEqual(b.fields, pyFields) && isDeepStrictEqual(b.fields, srv.content.fields),
  );
  check(
    "Y.Text content equal (browser, python, server)",
    b.notes === p.notes && b.notes === srv.content.notes,
    { length: b.notes.length, tokens: (b.notes.match(/\]/g) ?? []).length },
  );
  check(
    "no token lost or duplicated",
    (b.notes.match(/\[py\./g) ?? []).length === 48 &&
      (b.notes.match(/\[browser\./g) ?? []).length === 45,
  );

  // 8. Graceful leave: the peer sends an awareness null-state; the browser drops it at once.
  // (Events are matched by position, not time: Date.now() has 1 ms resolution.)
  const seenBefore = await page.evaluate(() => window.s2.presence.length);
  const leave = await call({ cmd: "disconnect", graceful: true });
  await sleep(500);
  const removed = (await page.evaluate(() => window.s2.presence))
    .slice(seenBefore)
    .filter((x) => x.event === "removed" && x.name === "py");
  result.timings_ms.graceful_leave_seen_by_browser = removed.length
    ? Math.max(0, Math.round(removed[0].at - leave.t_start))
    : null;
  check("graceful leave removes presence in browser", removed.length > 0, {
    ms: result.timings_ms.graceful_leave_seen_by_browser,
  });

  // 9. Abrupt leave: the browser keeps the ghost until the y-protocols 30 s outdated timeout.
  if (!opts["skip-abrupt"]) {
    await call({ cmd: "connect" });
    await sleep(500);
    const drop = await call({ cmd: "disconnect", graceful: false });
    let ghostMs = null;
    try {
      await page.waitForFunction(
        (since) =>
          window.s2.presence.some((x) => x.event === "removed" && x.name === "py" && x.at >= since),
        drop.t_start,
        { timeout: 45000, polling: 100 },
      );
      const ev = (await page.evaluate(() => window.s2.presence)).find(
        (x) => x.event === "removed" && x.name === "py" && x.at >= drop.t_start,
      );
      ghostMs = Math.round(ev.at - drop.t_start);
    } catch {
      ghostMs = null;
    }
    result.timings_ms.abrupt_leave_ghost_presence = ghostMs;
    check("abrupt leave eventually removes presence (timeout)", ghostMs !== null, { ms: ghostMs });
  }

  result.browser_console_errors = consoleErrors;
  check("no browser console errors", consoleErrors.length === 0, consoleErrors.slice(0, 5));
  result.versions = {
    yjs: version("yjs"),
    "y-websocket": version("y-websocket"),
    playwright: version("playwright"),
    chromium: browser.version(),
  };

  // 10. Tab close: y-websocket sends no awareness null-state on unload and pycrdt-websocket
  // does not clear a closed socket's awareness, so peers keep the ghost until their timeout.
  if (!opts["skip-abrupt"]) {
    await call({ cmd: "connect" });
    // The rejoining peer cannot see the browser until the browser re-announces (no awareness
    // snapshot on join), so nudge the browser's state and wait until the peer has it.
    await page.evaluate(() =>
      window.s2.provider.awareness.setLocalStateField("cursor", Date.now()),
    );
    const tSeen = Date.now();
    let seen = false;
    while (!seen && Date.now() - tSeen < 5000) {
      const { states } = await call({ cmd: "presence" });
      seen = Object.values(states).some((st) => st?.user?.name === "browser");
      if (!seen) await sleep(50);
    }
    check("rejoined peer sees browser after browser re-announces", seen);
    const tClose = Date.now();
    await page.close({ runBeforeUnload: true });
    let tabGhostMs = null;
    while (Date.now() - tClose < 45000) {
      const { removed } = await call({ cmd: "removed" });
      const hit = removed.find((r) => r.sender === "browser" && r.t >= tClose);
      if (hit) {
        tabGhostMs = Math.round(hit.t - tClose);
        break;
      }
      await sleep(200);
    }
    result.timings_ms.tab_close_ghost_presence_at_python = tabGhostMs;
    check("tab close eventually removes presence at python (timeout)", tabGhostMs !== null, {
      ms: tabGhostMs,
    });
  }
} catch (err) {
  check("browser check completed", false, String(err?.stack ?? err));
} finally {
  await call({ cmd: "quit" }).catch(() => {});
  peer.kill();
  if (browser) await browser.close();
  if (vite) await vite.close();
}

result.all_ok = Object.values(result.checks).every((c) => c.ok);
if (opts.out) writeFileSync(opts.out, JSON.stringify(result, null, 2));
console.log(
  JSON.stringify(
    {
      all_ok: result.all_ok,
      timings_ms: result.timings_ms,
      latency_python_to_browser_ms: result.latency_python_to_browser_ms,
      latency_browser_to_python_ms: result.latency_browser_to_python_ms,
      awareness_python_to_browser_ms: result.awareness_python_to_browser_ms,
      python_reads_browser_value_types: result.python_reads_browser_value_types,
      versions: result.versions,
    },
    null,
    2,
  ),
);
process.exit(result.all_ok ? 0 : 1);
