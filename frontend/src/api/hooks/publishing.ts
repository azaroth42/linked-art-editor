/**
 * TanStack Query hooks for checking and publishing a draft (Step 1.19): the JSON Schema of the
 * record's class, server validation of the live draft, the draft's semantic diff, publish and
 * discard. Validation and the diff read the live draft, so they are never cached.
 */
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, type Schemas, unwrap } from "../client.ts";

export type Diagnostic = Schemas["Diagnostic"];
export type Diagnostics = Schemas["Diagnostics"];
export type RecordSchema = Schemas["RecordSchema"];
export type SemanticDiff = Schemas["SemanticDiff"];
export type Change = SemanticDiff["changes"][number];
export type ChangeKind = Change["kind"];
export type ChangeStep = Schemas["Step"];
export type RefSummary = Schemas["RefSummary"];
export type AcceptedWarning = Schemas["AcceptedWarning"];
export type PublishResult = Schemas["PublishResult"];
export type DiscardResult = Schemas["DiscardResult"];

/** Longest edit summary `POST /api/records/{id}/publish` accepts (`SUMMARY_MAX_LENGTH`). */
export const SUMMARY_MAX_LENGTH = 2000;

/** `GET /api/profiles/schema/{class}`; the vendored schemas change only with a deployment. */
export function useRecordSchema(recordClass: string | null) {
  return useQuery({
    queryKey: ["profiles", "schema", recordClass],
    queryFn: () =>
      unwrap(
        api.GET("/api/profiles/schema/{record_class}", {
          params: { path: { record_class: recordClass ?? "" } },
        }),
      ),
    enabled: recordClass !== null,
    staleTime: Infinity,
    retry: false,
  });
}

/** `POST /api/records/{id}/draft/validate`: every diagnostic of the live draft. */
export function validateDraft(id: string): Promise<Diagnostics> {
  return unwrap(
    api.POST("/api/records/{record_id}/draft/validate", {
      params: { path: { record_id: id } },
    }),
  );
}

/** `POST /api/records/{id}/draft/diff`: the changes from the published version to the draft. */
export function fetchDraftDiff(id: string): Promise<SemanticDiff> {
  return unwrap(
    api.POST("/api/records/{record_id}/draft/diff", { params: { path: { record_id: id } } }),
  );
}

/** What the Publish dialog shows: the draft's diagnostics and its changes, read together. */
export interface DraftReview {
  diagnostics: Diagnostic[];
  diff: SemanticDiff;
}

/** Server validation and the semantic diff of the live draft, fetched while `enabled`. */
export function useDraftReview(id: string, enabled: boolean) {
  return useQuery({
    queryKey: ["records", id, "draft", "review"],
    queryFn: async (): Promise<DraftReview> => {
      const [diagnostics, diff] = await Promise.all([validateDraft(id), fetchDraftDiff(id)]);
      return { diagnostics: diagnostics.items, diff };
    },
    enabled,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/** `POST /api/records/{id}/publish`. */
export function usePublish(id: string) {
  return useMutation({
    mutationFn: (body: { summary: string; accepted_warnings: AcceptedWarning[] }) =>
      unwrap(
        api.POST("/api/records/{record_id}/publish", {
          params: { path: { record_id: id } },
          body,
        }),
      ),
  });
}

/** `POST /api/records/{id}/draft/discard`. */
export function useDiscard(id: string) {
  return useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/records/{record_id}/draft/discard", {
          params: { path: { record_id: id } },
        }),
      ),
  });
}
