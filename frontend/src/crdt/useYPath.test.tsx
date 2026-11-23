import { act, cleanup, render, renderHook, screen } from "@testing-library/react";
import { Suspense } from "react";
import { I18nextProvider } from "react-i18next";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createI18n } from "../i18n/index.ts";
import { stubLocaleFetch } from "../i18n/testing.ts";
import { hydrate } from "./hydrate.ts";
import { addNode, listEntries, moveEntry, setScalar } from "./paths.ts";
import { RawInspector, REFRESH_INTERVAL } from "./RawInspector.tsx";
import { useYPath } from "./useYPath.ts";

const INSPECTOR_ID = "inspector";
const URI = "https://example.org/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";

function record() {
  return hydrate({
    id: URI,
    type: "HumanMadeObject",
    identified_by: [
      { type: "Name", content: "a" },
      { type: "Name", content: "b" },
    ],
    classified_as: [{ id: "http://vocab.getty.edu/aat/1", type: "Type" }],
  });
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useYPath", () => {
  it("re-renders when what the path addresses changes, and only then", () => {
    const doc = record();
    let renders = 0;
    const { result } = renderHook(() => {
      renders += 1;
      return useYPath(doc, "identified_by[Name].content");
    });
    expect(result.current.json).toEqual(["a", "b"]);
    const before = renders;

    act(() => setScalar(doc, "x_note", "elsewhere"));
    act(() => setScalar(doc, "classified_as.content", "elsewhere"));
    expect(renders).toBe(before);

    const [first] = listEntries(doc, "identified_by")[0] ?? [];
    act(() => setScalar(doc, "content", "A", { base: listEntries(doc, "identified_by")[0]?.[1] }));
    expect(result.current.json).toEqual(["A", "b"]);
    act(() => addNode(doc, "identified_by[Name]", { content: "c" }));
    expect(result.current.json).toEqual(["A", "b", "c"]);
    act(() => moveEntry(doc, "identified_by", first as string, null));
    expect(result.current.json).toEqual(["b", "c", "A"]);
    act(() => addNode(doc, "identified_by[Identifier]", { content: "not a name" }));
    expect(result.current.json).toEqual(["b", "c", "A"]);
  });

  it("reads from a base map and reports a malformed path", () => {
    const doc = record();
    const base = listEntries(doc, "identified_by")[1]?.[1] ?? null;
    const { result } = renderHook(() => useYPath(doc, "content", { base }));
    expect(result.current.json).toEqual(["b"]);
    const bad = renderHook(() => useYPath(doc, "identified_by[0]"));
    expect(bad.result.current.error).not.toBeNull();
    expect(renderHook(() => useYPath(null, "content")).result.current.values).toEqual([]);
  });
});

describe("RawInspector", () => {
  it("shows the projection and refreshes at most once per second", async () => {
    stubLocaleFetch();
    const i18n = createI18n("en");
    const doc = record();
    render(
      <I18nextProvider i18n={i18n}>
        <Suspense fallback={null}>
          <RawInspector doc={doc} id={INSPECTOR_ID} />
        </Suspense>
      </I18nextProvider>,
    );
    const pre = await screen.findByTestId("raw-jsonld");
    vi.useFakeTimers();
    expect(pre.textContent).toContain('"content": "a"');

    act(() => {
      vi.advanceTimersByTime(REFRESH_INTERVAL);
      setScalar(doc, "x_note", "one");
    });
    act(() => vi.advanceTimersByTime(0));
    expect(pre.textContent).toContain('"x_note": "one"');

    act(() => setScalar(doc, "x_note", "two"));
    act(() => vi.advanceTimersByTime(REFRESH_INTERVAL / 2));
    expect(pre.textContent).toContain('"x_note": "one"');
    act(() => setScalar(doc, "x_note", "three"));
    act(() => vi.advanceTimersByTime(REFRESH_INTERVAL / 2));
    expect(pre.textContent).toContain('"x_note": "three"');
  });
});
