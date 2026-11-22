/** TanStack Query hooks for the `/auth` resource: the signed-in user and logout. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, type Schemas, unwrap } from "../client.ts";

/** Body of `GET /auth/me`. */
export type Me = Schemas["Me"];

export const meQueryKey = ["auth", "me"] as const;

/** `GET /auth/me`; `null` when the request is anonymous (401), any other failure throws. */
export async function fetchMe(): Promise<Me | null> {
  try {
    return await unwrap(api.GET("/auth/me"));
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return null;
    }
    throw error;
  }
}

/** The caller of the API: the session's user, the dev user, or `null` when signed out. */
export function useMe() {
  return useQuery({ queryKey: meQueryKey, queryFn: fetchMe, staleTime: 5 * 60_000 });
}

/**
 * `POST /auth/logout`. On success every cached query is reset, so no data fetched as the
 * previous user survives, and `/auth/me` is fetched again.
 */
export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/auth/logout")),
    onSuccess: () => queryClient.resetQueries(),
  });
}
