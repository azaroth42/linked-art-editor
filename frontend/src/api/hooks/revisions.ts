/**
 * TanStack Query hooks for a record's published versions (Step 1.20; contract 1.14): the
 * revision list, the semantic diff of two versions, and revert. Published versions never change,
 * so a diff is cached; the list grows with every publish and is read afresh on mount.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Schemas, unwrap } from "../client.ts";
import { editorRecordKey } from "./records.ts";

export type RevisionSummary = Schemas["RevisionSummary"];
export type Reverted = Schemas["Reverted"];

/** `GET /api/records/{id}`: the record as it is published now (its class and URI). */
export function useCurrentRecord(id: string) {
  return useQuery({
    queryKey: ["records", id, "current"],
    queryFn: async () =>
      (await unwrap(
        api.GET("/api/records/{record_id}", { params: { path: { record_id: id } } }),
      )) as Record<string, unknown>,
    retry: false,
  });
}

/** `GET /api/records/{id}/revisions`: every published version, oldest first. */
export function useRevisions(id: string) {
  return useQuery({
    queryKey: ["records", id, "revisions"],
    queryFn: () =>
      unwrap(
        api.GET("/api/records/{record_id}/revisions", { params: { path: { record_id: id } } }),
      ),
    refetchOnMount: "always",
    retry: false,
  });
}

/**
 * `GET /api/records/{id}/revisions/{from}/diff/{to}`: the changes that turn version `from` into
 * version `to` (either may be the later one). Not fetched while either is `null`.
 */
export function useRevisionDiff(id: string, from: number | null, to: number | null) {
  return useQuery({
    queryKey: ["records", id, "revisions", "diff", from, to],
    queryFn: () =>
      unwrap(
        api.GET("/api/records/{record_id}/revisions/{from_version}/diff/{to_version}", {
          params: { path: { record_id: id, from_version: from ?? 0, to_version: to ?? 0 } },
        }),
      ),
    enabled: from !== null && to !== null,
    staleTime: Infinity,
    retry: false,
  });
}

/**
 * `POST /api/records/{id}/revisions/{n}/revert` (needs `publish`): the draft is replaced by
 * version `n` under a new epoch. Nothing is published. The editor's cached record (and so its
 * epoch) is dropped, so the editor opens the new draft rather than the ended one.
 */
export function useRevert(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (version: number) =>
      unwrap(
        api.POST("/api/records/{record_id}/revisions/{version}/revert", {
          params: { path: { record_id: id, version } },
        }),
      ),
    onSuccess: () => queryClient.removeQueries({ queryKey: editorRecordKey(id) }),
  });
}
