/** TanStack Query hooks for the `/api/tokens` resource: Personal Access Tokens of the caller. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas, unwrap } from "../client.ts";

export type TokenInfo = Schemas["TokenInfo"];
export type TokenCreate = Schemas["TokenCreate"];
/** Response of `POST /api/tokens`: the only time the plaintext `token` is available. */
export type TokenCreated = Schemas["TokenCreated"];

export const tokensQueryKey = ["tokens"] as const;

/** `GET /api/tokens`: the caller's tokens, without their secrets. */
export function useTokens() {
  return useQuery({
    queryKey: tokensQueryKey,
    queryFn: () => unwrap(api.GET("/api/tokens")),
  });
}

/** `POST /api/tokens`; the result holds the plaintext token, which is never shown again. */
export function useCreateToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: TokenCreate) => unwrap(api.POST("/api/tokens", { body })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: tokensQueryKey }),
  });
}

/** `DELETE /api/tokens/{token_id}`: revoke one of the caller's tokens. */
export function useDeleteToken() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (tokenId: string) =>
      unwrap(api.DELETE("/api/tokens/{token_id}", { params: { path: { token_id: tokenId } } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: tokensQueryKey }),
  });
}
