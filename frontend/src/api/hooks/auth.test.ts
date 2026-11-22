import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../client.ts";
import { anonymous, EDITOR, errorResponse, stubFetch } from "../testing.ts";
import { fetchMe } from "./auth.ts";

afterEach(() => vi.unstubAllGlobals());

describe("fetchMe", () => {
  it("returns the signed-in user", async () => {
    stubFetch({ "GET /auth/me": () => Response.json(EDITOR) });
    await expect(fetchMe()).resolves.toEqual(EDITOR);
  });

  it("returns null when the request is anonymous", async () => {
    stubFetch({ "GET /auth/me": anonymous });
    await expect(fetchMe()).resolves.toBeNull();
  });

  it("throws any other failure", async () => {
    stubFetch({ "GET /auth/me": () => errorResponse(503, "LAE-X-001", "/auth/me") });
    await expect(fetchMe()).rejects.toBeInstanceOf(ApiError);
  });
});
