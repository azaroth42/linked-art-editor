/** The TanStack Query client shared by the whole SPA. */
import { QueryClient } from "@tanstack/react-query";

import { ApiError } from "./client.ts";

/** Attempts after the first for a failed query (network errors and 5xx only). */
export const MAX_QUERY_RETRIES = 2;

/** Retry network failures and server errors, never a 4xx: repeating it cannot succeed. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) {
    return false;
  }
  return failureCount < MAX_QUERY_RETRIES;
}

/** Create the query client with the application's defaults. */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: shouldRetry, staleTime: 30_000 },
      mutations: { retry: false },
    },
  });
}
