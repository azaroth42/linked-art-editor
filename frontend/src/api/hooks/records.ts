/** TanStack Query hooks for `/api/records`. */
import { useQuery } from "@tanstack/react-query";

import { api, ApiError } from "../client.ts";

/** Response header of `GET /api/records/{id}` with the record's current draft epoch. */
export const DRAFT_EPOCH_HEADER = "X-Draft-Epoch";

/** What the editor needs to open a record: its JSON-LD and the epoch to connect with. */
export interface EditorRecord {
  jsonld: Record<string, unknown>;
  draftEpoch: number;
}

/** `GET /api/records/{id}`: the JSON-LD and the `X-Draft-Epoch` header. */
export async function fetchEditorRecord(id: string): Promise<EditorRecord> {
  const { data, error, response } = await api.GET("/api/records/{record_id}", {
    params: { path: { record_id: id } },
  });
  if (!response.ok) {
    throw new ApiError(response.status, error, `/api/records/${id}`);
  }
  const epoch = Number(response.headers.get(DRAFT_EPOCH_HEADER));
  if (!Number.isInteger(epoch)) {
    throw new ApiError(response.status, null, `/api/records/${id}`);
  }
  return { jsonld: data as Record<string, unknown>, draftEpoch: epoch };
}

/**
 * The record for the editor. The epoch changes on publish, discard and revert (ADR-0027), so it
 * is fetched afresh whenever the editor mounts and never refreshed in the background: a live
 * document must not move to another epoch (a new epoch means a reload).
 */
export function useEditorRecord(id: string) {
  return useQuery({
    queryKey: ["records", id, "editor"],
    queryFn: () => fetchEditorRecord(id),
    staleTime: Infinity,
    gcTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
}
