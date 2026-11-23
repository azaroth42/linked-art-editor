/**
 * TanStack Query hooks for what the editor loads besides the record (Step 1.18): the UI Profile
 * of the record's class, the Value Set bundle, autocomplete, and creating a stub record.
 */
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, type Schemas, unwrap } from "../client.ts";

export type UiProfile = Schemas["UiProfile"];
export type ProfileSection = Schemas["ProfileSection"];
export type ProfileSlot = Schemas["ProfileSlot"];
export type VocabBundle = Schemas["VocabBundle"];
export type BundleVocabulary = Schemas["BundleVocabulary"];
export type BundleTerm = Schemas["BundleTerm"];
export type AutocompleteResult = Schemas["AutocompleteResult"];
export type RecordCreated = Schemas["RecordCreated"];

/** Fewest characters `GET /api/search/autocomplete` answers (`lae.search.autocomplete`). */
export const AUTOCOMPLETE_MIN_LENGTH = 2;

/** `GET /api/profiles/{class}`; profiles change only with a deployment. */
export function useProfile(recordClass: string | null) {
  return useQuery({
    queryKey: ["profiles", recordClass],
    queryFn: () =>
      unwrap(
        api.GET("/api/profiles/{record_class}", {
          params: { path: { record_class: recordClass ?? "" } },
        }),
      ),
    enabled: recordClass !== null,
    staleTime: Infinity,
  });
}

/**
 * `GET /api/vocab/bundle`. The server answers `Cache-Control: no-cache` with an `ETag`, so the
 * browser revalidates with `If-None-Match` and a 304 costs no body.
 */
export function useVocabBundle() {
  return useQuery({
    queryKey: ["vocab", "bundle"],
    queryFn: () => unwrap(api.GET("/api/vocab/bundle")),
    staleTime: 5 * 60 * 1000,
  });
}

/** `GET /api/search/autocomplete`: published entities of `types` whose name starts with `q`. */
export function fetchAutocomplete(
  q: string,
  types: readonly string[],
  signal?: AbortSignal,
): Promise<AutocompleteResult[]> {
  return unwrap(
    api.GET("/api/search/autocomplete", {
      params: { query: { q, types: types.join(","), limit: 10 } },
      signal,
    }),
  );
}

/** `GET /api/search/autocomplete` for what has been typed so far (nothing below the minimum). */
export function useAutocomplete(q: string, types: readonly string[]) {
  const query = q.trim();
  return useQuery({
    queryKey: ["autocomplete", query, types],
    queryFn: ({ signal }) => fetchAutocomplete(query, types, signal),
    enabled: query.length >= AUTOCOMPLETE_MIN_LENGTH,
    staleTime: 30 * 1000,
    retry: false,
  });
}

/** `POST /api/records` with a one-name template: a stub to reference from the record being edited. */
export function useCreateStub() {
  return useMutation({
    mutationFn: ({ type, name }: { type: string; name: string }) =>
      unwrap(
        api.POST("/api/records", {
          body: { type, template: { identified_by: [{ type: "Name", content: name }] } },
        }),
      ),
  });
}
