import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { errorResponse, stubFetch } from "../api/testing.ts";
import { draft, renderWidget } from "../widgets/testing.tsx";
import { DiscardButton } from "./DiscardButton.tsx";

const ID = "0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
const DISCARD = `POST /api/records/${ID}/draft/discard`;

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function renderButton() {
  const props = { onStart: vi.fn(), onDiscarded: vi.fn(), onFailed: vi.fn() };
  renderWidget(<DiscardButton recordId={ID} draftEpoch={4} disabled={false} {...props} />, draft());
  return props;
}

describe("DiscardButton", () => {
  it("asks first, starting on the safe choice, and does nothing when kept", async () => {
    const user = userEvent.setup();
    const requests = stubFetch({});
    const { onStart } = renderButton();
    await user.click(await screen.findByRole("button", { name: "Discard draft" }));
    const dialog = screen.getByRole("dialog", { name: "Discard this draft?" });
    expect(within(dialog).getByRole("button", { name: "Keep editing" })).toBe(
      document.activeElement,
    );
    await user.click(within(dialog).getByRole("button", { name: "Keep editing" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(onStart).not.toHaveBeenCalled();
    expect(requests).toEqual([]);
  });

  it("discards the draft when confirmed", async () => {
    const user = userEvent.setup();
    const result = { id: ID, deleted: false, draft_epoch: 5 };
    const requests = stubFetch({ [DISCARD]: () => Response.json(result) });
    const { onStart, onDiscarded } = renderButton();
    await user.click(await screen.findByRole("button", { name: "Discard draft" }));
    await user.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(onDiscarded).toHaveBeenCalledWith(result));
    // Only the draft on screen may be discarded: the request names its epoch (issue #178).
    expect(await requests[0]?.json()).toEqual({ draft_epoch: 4 });
    expect(onStart).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("explains a refusal and reports it", async () => {
    const user = userEvent.setup();
    stubFetch({
      [DISCARD]: () =>
        errorResponse(403, "LAE-AUTH-001", `/api/records/${ID}/draft/discard`, {
          missing: ["publish"],
        }),
    });
    const { onFailed, onDiscarded } = renderButton();
    await user.click(await screen.findByRole("button", { name: "Discard draft" }));
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(
      await screen.findByText(
        "You do not have permission to do this. Missing permissions: publish.",
      ),
    ).toBeTruthy();
    expect(onFailed).toHaveBeenCalledOnce();
    expect(onDiscarded).not.toHaveBeenCalled();
  });
});
