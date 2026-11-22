import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../client.ts";
import { anonymous, EDITOR, errorResponse, stubFetch } from "../testing.ts";
import { fetchLoginProviders, fetchMe } from "./auth.ts";

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

describe("fetchLoginProviders", () => {
  it("returns the providers in the server's order", async () => {
    const providers = [
      { id: "oidc", display_name: "OpenID Connect" },
      { id: "github", display_name: "GitHub" },
    ];
    stubFetch({ "GET /auth/providers": () => Response.json(providers) });
    await expect(fetchLoginProviders()).resolves.toEqual(providers);
  });

  it("throws when the server cannot answer", async () => {
    stubFetch({ "GET /auth/providers": () => errorResponse(502, "LAE-X-001", "/auth/providers") });
    await expect(fetchLoginProviders()).rejects.toBeInstanceOf(ApiError);
  });
});
