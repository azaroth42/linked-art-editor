import { describe, expect, it } from "vitest";

import { ApiError } from "./client.ts";
import { MAX_QUERY_RETRIES, shouldRetry } from "./queryClient.ts";

describe("shouldRetry", () => {
  it("never retries a client error", () => {
    expect(shouldRetry(0, new ApiError(404, null))).toBe(false);
    expect(shouldRetry(0, new ApiError(401, null))).toBe(false);
  });

  it("retries server and network errors a bounded number of times", () => {
    expect(shouldRetry(0, new ApiError(503, null))).toBe(true);
    expect(shouldRetry(0, new TypeError("Failed to fetch"))).toBe(true);
    expect(shouldRetry(MAX_QUERY_RETRIES, new ApiError(503, null))).toBe(false);
  });
});
