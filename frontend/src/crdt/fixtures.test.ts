/**
 * The shared operation fixtures of `backend/tests/projection/fixtures/`, run unchanged against
 * the TypeScript mirror. Python (`fixture_runner.py`) and TypeScript must leave the same
 * document behind for every step: every `returns`, `error`, `expected` and `expected_state` is
 * reproduced exactly. A mismatch is a bug in the mirror, never in the fixtures.
 */
import { describe, expect, it } from "vitest";
import * as Y from "yjs";

import { project } from "./project.ts";
import { ProjectionError } from "./rules.ts";
import {
  callOperation,
  canonical,
  docState,
  type Fixture,
  fromBase64,
  loadFixtures,
  replica,
  syncDocs,
} from "./testing.ts";

const FIXTURES = loadFixtures();

/** Run every step of one fixture and check every expectation; returns the client docs. */
function runFixture(fixture: Fixture): Map<string, Y.Doc> {
  const update = fromBase64(fixture.initial_update);
  const docs = new Map<string, Y.Doc>();
  for (const [name, clientId] of Object.entries(fixture.clients)) {
    const doc = replica(update, clientId);
    expect(doc.clientID).toBe(clientId);
    expect(canonical(docState(doc))).toBe(canonical(fixture.initial_state));
    expect(canonical(project(doc))).toBe(canonical(fixture.initial));
    docs.set(name, doc);
  }

  fixture.steps.forEach((step, number) => {
    const where = `step ${String(number)}: ${JSON.stringify(step).slice(0, 200)}`;
    if (step.sync !== undefined) {
      syncDocs(step.sync.map((name) => docs.get(name) as Y.Doc));
      return;
    }
    const doc = docs.get(step.client as string) as Y.Doc;
    const before = canonical(docState(doc));
    const beforeUpdate = Y.encodeStateAsUpdate(doc);
    let outcome: { result: unknown; unused: string[] };
    try {
      outcome = callOperation(doc, step.op as string, step.args ?? {});
    } catch (error) {
      if (!(error instanceof ProjectionError)) {
        throw error;
      }
      // A failed operation leaves the document as it was (R25).
      expect(canonical(docState(doc)), where).toBe(before);
      expect(Y.encodeStateAsUpdate(doc), where).toEqual(beforeUpdate);
      expect(error.code, `${error.message}; ${where}`).toBe(step.error);
      return;
    }
    expect(step.error, `expected ${String(step.error)}; ${where}`).toBeUndefined();
    expect(outcome.unused, `the operation minted fewer node IRIs than listed; ${where}`).toEqual(
      [],
    );
    // Through JSON, as the file stores the result.
    expect(canonical(outcome.result ?? null), where).toBe(canonical(step.returns));
  });
  return docs;
}

describe("the shared projection fixtures", () => {
  it("are all eleven files of backend/tests/projection/fixtures", () => {
    expect(FIXTURES.map(([stem]) => stem)).toEqual([
      "concurrent-first-inserts",
      "intermediate-nodes",
      "move-and-remove",
      "order-append-midpoint-first",
      "rebalance-after-tie",
      "rebalance-exhausted-gap",
      "repeated-ids",
      "scalars-last-writer-wins",
      "single-value-and-entries",
      "text-code-points",
      "text-surrogates-and-combining",
    ]);
  });

  it.each(FIXTURES)("%s: every step, projection and state as in Python", (_, fixture) => {
    const docs = runFixture(fixture);
    for (const [name, doc] of docs) {
      // Byte-identical canonical projections and state dumps on every client.
      expect(canonical(project(doc)), `client ${name} projection`).toBe(
        canonical(fixture.expected),
      );
      expect(canonical(docState(doc)), `client ${name} state`).toBe(
        canonical(fixture.expected_state),
      );
    }
  });
});
