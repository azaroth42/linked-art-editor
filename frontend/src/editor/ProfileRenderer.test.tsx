import { cleanup, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { stubFetch } from "../api/testing.ts";
import { BUNDLE, draft, ORIGIN, renderWidget } from "../widgets/testing.tsx";
import { ProfileRenderer } from "./ProfileRenderer.tsx";
import type { SlotFocusRequest } from "./slotFocus.ts";
import { PROFILE } from "./validation/testing.ts";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const TITLE: SlotFocusRequest = { slotId: "title", nonce: 1 };
const DIMENSIONS: SlotFocusRequest = { slotId: "dimensions", nonce: 1 };

/** The renderer with a focus request, as the editor page holds it. */
function Harness({ initial, onDone }: { initial: SlotFocusRequest; onDone: () => void }) {
  const [request, setRequest] = useState<SlotFocusRequest | null>(initial);
  const [doc] = useState(() => draft({ identified_by: [{ type: "Name", content: "A vase" }] }));
  return (
    <>
      <ProfileRenderer
        profile={PROFILE}
        doc={doc}
        origin={ORIGIN}
        disabled={false}
        bundle={BUNDLE}
        refLabels={new Map()}
        focus={{
          request,
          done: () => {
            setRequest(null);
            onDone();
          },
        }}
      />
    </>
  );
}

describe("ProfileRenderer focus requests", () => {
  it("focuses the first control of the slot a diagnostic asks for", async () => {
    stubFetch({});
    const onDone = vi.fn();
    renderWidget(<Harness initial={TITLE} onDone={onDone} />, draft());
    const title = await screen.findByLabelText("Title 1");
    await waitFor(() => expect(document.activeElement).toBe(title));
    expect(onDone).toHaveBeenCalledOnce();
  });

  it("opens the details a slot waits behind before focusing it", async () => {
    stubFetch({});
    const onDone = vi.fn();
    renderWidget(<Harness initial={DIMENSIONS} onDone={onDone} />, draft());
    await waitFor(() => expect(onDone).toHaveBeenCalledOnce());
    expect(screen.getByRole("button", { name: "Hide details" }).getAttribute("aria-expanded")).toBe(
      "true",
    );
    const frame = document.querySelector<HTMLElement>('[data-slot="dimensions"]');
    expect(frame).not.toBeNull();
    expect(frame?.contains(document.activeElement)).toBe(true);
  });
});
