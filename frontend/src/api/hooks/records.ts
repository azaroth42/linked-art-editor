/** TanStack Query hooks for `/api/records`. */
import { useQuery } from "@tanstack/react-query";

import { api, ApiError } from "../client.ts";

/** Response header of `GET /api/records/{id}` with the record's current draft epoch. */
export const DRAFT_EPOCH_HEADER = "X-Draft-Epoch";

/**
 * Response header of `GET /api/records/{id}` with the installation's label language
 * (`LAE_LABEL_LANGUAGE`, Step 1.19a): the language the draft's root `_label` is derived in.
 */
export const LABEL_LANGUAGE_HEADER = "X-Label-Language";

/** A label language as the server sends it: a lower-case primary language subtag. */
const LABEL_LANGUAGE = /^[a-z]{2,3}$/;

/**
 * What the editor needs to open a record: its JSON-LD, the epoch to connect with, and the
 * language the draft's root `_label` is derived in (`projectDraft`).
 */
export interface EditorRecord {
  jsonld: Record<string, unknown>;
  draftEpoch: number;
  labelLanguage: string;
}

/**
 * `GET /api/records/{id}`: the JSON-LD and the `X-Draft-Epoch` and `X-Label-Language` headers.
 * A response without a valid header is an `ApiError`: the editor never guesses either value.
 */
export async function fetchEditorRecord(id: string): Promise<EditorRecord> {
  const { data, error, response } = await api.GET("/api/records/{record_id}", {
    params: { path: { record_id: id } },
  });
  if (!response.ok) {
    throw new ApiError(response.status, error, `/api/records/${id}`);
  }
  const epoch = Number(response.headers.get(DRAFT_EPOCH_HEADER));
  const labelLanguage = response.headers.get(LABEL_LANGUAGE_HEADER);
  if (!Number.isInteger(epoch) || labelLanguage === null || !LABEL_LANGUAGE.test(labelLanguage)) {
    throw new ApiError(response.status, null, `/api/records/${id}`);
  }
  return { jsonld: data as Record<string, unknown>, draftEpoch: epoch, labelLanguage };
}

/** The query key of {@link useEditorRecord}. */
export function editorRecordKey(id: string) {
  return ["records", id, "editor"] as const;
}

/**
 * The record for the editor. The epoch changes on publish, discard and revert (ADR-0027), so it
 * is fetched afresh whenever the editor mounts and never refreshed in the background: a live
 * document must not move to another epoch (a new epoch means a reload).
 */
export function useEditorRecord(id: string) {
  return useQuery({
    queryKey: editorRecordKey(id),
    queryFn: () => fetchEditorRecord(id),
    staleTime: Infinity,
    gcTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    retry: false,
  });
}
