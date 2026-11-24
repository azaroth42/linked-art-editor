/** `fetchEditorRecord`: the record, its draft epoch and its label language (Step 1.19a). */
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../client.ts";
import { stubFetch } from "../testing.ts";
import { DRAFT_EPOCH_HEADER, fetchEditorRecord, LABEL_LANGUAGE_HEADER } from "./records.ts";

const ID = "0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
const JSONLD = { id: "https://data.test/id/1", type: "HumanMadeObject" };

function answer(headers: Record<string, string>) {
  stubFetch({ [`GET /api/records/${ID}`]: () => Response.json(JSONLD, { headers }) });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchEditorRecord", () => {
  it("fetchEditorRecord returns the label language of the X-Label-Language header", async () => {
    expect(LABEL_LANGUAGE_HEADER).toBe("X-Label-Language");
    for (const language of ["fr", "en", "de"]) {
      answer({ [DRAFT_EPOCH_HEADER]: "4", [LABEL_LANGUAGE_HEADER]: language });
      await expect(fetchEditorRecord(ID)).resolves.toEqual({
        jsonld: JSONLD,
        draftEpoch: 4,
        labelLanguage: language,
      });
    }
  });

  it("fetchEditorRecord refuses a response without a valid X-Label-Language", async () => {
    // (Header values are trimmed by fetch, so " en" would arrive as "en".)
    const bad: (string | null)[] = [null, "", "EN", "en-GB", "e", "english", "und-x"];
    for (const value of bad) {
      answer(
        value === null
          ? { [DRAFT_EPOCH_HEADER]: "4" }
          : { [DRAFT_EPOCH_HEADER]: "4", [LABEL_LANGUAGE_HEADER]: value },
      );
      await expect(fetchEditorRecord(ID), String(value)).rejects.toBeInstanceOf(ApiError);
    }
  });
});
