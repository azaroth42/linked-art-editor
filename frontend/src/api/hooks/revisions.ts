/**
 * TanStack Query hooks for a record's published versions (Step 1.20; contract 1.14): the
 * revision list, the semantic diff of two versions, and revert. Published versions never change,
 * so a diff is cached; the list grows with every publish and is read afresh on mount.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, type Schemas, unwrap } from "../client.ts";
import { DRAFT_EPOCH_HEADER, editorRecordKey } from "./records.ts";

export type RevisionSummary = Schemas["RevisionSummary"];
export type Reverted = Schemas["Reverted"];

/** The record as it is published now, and the draft epoch it had when it was read. */
export interface CurrentRecord {
  jsonld: Record<string, unknown>;
  /** `X-Draft-Epoch` (`records.draft_epoch`); `null` if the answer did not carry a valid one. */
  draftEpoch: number | null;
}

/** The query key of {@link useCurrentRecord}. */
export function currentRecordKey(id: string) {
  return ["records", id, "current"] as const;
}

/**
 * `GET /api/records/{id}`: the record as it is published now (its class and URI) and its draft
 * epoch, which a revert states so that it can only replace the draft the page knew about. Read
 * afresh on every mount and never from the cache: the epoch changes on publish, discard and
 * revert.
 */
export function useCurrentRecord(id: string) {
  return useQuery({
    queryKey: currentRecordKey(id),
    queryFn: async (): Promise<CurrentRecord> => {
      const { data, error, response } = await api.GET("/api/records/{record_id}", {
        params: { path: { record_id: id } },
      });
      if (!response.ok) {
        throw new ApiError(response.status, error, `/api/records/${id}`);
      }
      const header = response.headers.get(DRAFT_EPOCH_HEADER);
      const epoch = header === null || header === "" ? NaN : Number(header);
      return {
        jsonld: data as Record<string, unknown>,
        draftEpoch: Number.isInteger(epoch) ? epoch : null,
      };
    },
    gcTime: 0,
    refetchOnMount: "always",
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

/** What a revert is asked: the version, and the draft epoch the page read (`null`: unknown). */
export interface RevertRequest {
  version: number;
  draftEpoch: number | null;
}

/**
 * `POST /api/records/{id}/revisions/{n}/revert` (needs `publish`): the draft is replaced by
 * version `n` under a new epoch. Nothing is published. The editor's cached record (and so its
 * epoch) is dropped, so the editor opens the new draft rather than the ended one.
 *
 * The request names the draft epoch the page read, so the server replaces only the draft the
 * page knew about and answers 409 `LAE-CRDT-007` if the record was published or its draft
 * discarded since (issue #178). After a failure the record and the version list are read again:
 * the page then shows what there is now, and a second attempt names the current epoch.
 */
export function useRevert(id: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ version, draftEpoch }: RevertRequest) =>
      unwrap(
        api.POST("/api/records/{record_id}/revisions/{version}/revert", {
          params: { path: { record_id: id, version } },
          body: draftEpoch === null ? undefined : { draft_epoch: draftEpoch },
        }),
      ),
    onSuccess: () => queryClient.removeQueries({ queryKey: editorRecordKey(id) }),
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: currentRecordKey(id) });
      void queryClient.invalidateQueries({ queryKey: ["records", id, "revisions"], exact: true });
    },
  });
}
