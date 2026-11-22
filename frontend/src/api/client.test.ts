import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, createApiClient, httpErrorCode, unwrap } from "./client.ts";
import { EDITOR, errorResponse, stubFetch } from "./testing.ts";

afterEach(() => vi.unstubAllGlobals());

describe("api", () => {
  it("calls the page's origin with credentials and returns the typed body", async () => {
    const requests = stubFetch({ "GET /auth/me": () => Response.json(EDITOR) });
    const me = await unwrap(api.GET("/auth/me"));
    expect(me.display_name).toBe("editor@example.org");
    expect(requests).toHaveLength(1);
    const [request] = requests;
    expect(request?.url).toBe(`${globalThis.location.origin}/auth/me`);
    expect(request?.credentials).toBe("include");
    expect(request?.headers.get("Accept")).toBe("application/json");
  });

  it("fills path parameters and sends JSON bodies", async () => {
    const requests = stubFetch({
      "POST /api/tokens": () => Response.json({}, { status: 201 }),
      "DELETE /api/tokens/abc": () => new Response(null, { status: 204 }),
    });
    await unwrap(api.POST("/api/tokens", { body: { name: "ci", scopes: ["edit_draft"] } }));
    const deleted = await unwrap(
      api.DELETE("/api/tokens/{token_id}", { params: { path: { token_id: "abc" } } }),
    );
    expect(deleted).toBeUndefined();
    expect(await requests[0]?.json()).toEqual({ name: "ci", scopes: ["edit_draft"] });
    expect(requests[1]?.method).toBe("DELETE");
  });
});

describe("unwrap", () => {
  it("throws an ApiError carrying the structured error body", async () => {
    stubFetch({
      "GET /api/tokens": () =>
        errorResponse(403, "LAE-AUTH-001", "/api/tokens", { missing: ["publish"] }),
    });
    const error: unknown = await unwrap(api.GET("/api/tokens")).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 403,
      code: "LAE-AUTH-001",
      path: "/api/tokens",
      params: { missing: ["publish"] },
      message: "LAE-AUTH-001",
    });
  });

  it("gives an unstructured error body an HTTP code and the request path", async () => {
    const detail = { detail: [{ loc: ["body", "name"], msg: "Field required" }] };
    const client = createApiClient({
      baseUrl: "http://api.test",
      fetch: async (request) => {
        const response = Response.json(detail, { status: 422 });
        Object.defineProperty(response, "url", { value: request.url });
        return response;
      },
    });
    const error = await unwrap(
      client.POST("/api/tokens", { body: { name: "", scopes: [] } }),
    ).catch((e: unknown) => e);
    expect(error).toMatchObject({
      status: 422,
      code: httpErrorCode(422),
      path: "/api/tokens",
      params: {},
      body: detail,
    });
  });

  it("lets network failures through unchanged", async () => {
    const failure = new TypeError("Failed to fetch");
    const client = createApiClient({
      baseUrl: "http://api.test",
      fetch: () => Promise.reject(failure),
    });
    await expect(unwrap(client.GET("/auth/me"))).rejects.toBe(failure);
  });
});
