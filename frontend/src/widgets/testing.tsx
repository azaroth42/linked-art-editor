/** Test helpers for the widgets: a live document, a small bundle, and a rendering harness. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { type ReactNode, Suspense } from "react";
import { I18nextProvider } from "react-i18next";
import type * as Y from "yjs";

import type { ProfileSlot, VocabBundle } from "../api/hooks/editing.ts";
import { hydrate } from "../crdt/hydrate.ts";
import { project } from "../crdt/project.ts";
import { createI18n, DEFAULT_LANGUAGE } from "../i18n/index.ts";
import { type WidgetEnv, WidgetEnvContext } from "./environment.ts";

export const RECORD_URI = "https://data.test/id/0192f0c1-7b1e-7c3a-9d2e-5a1b2c3d4e5f";
export const AAT = "http://vocab.getty.edu/aat/";
export const PRIMARY_NAME = `${AAT}300404670`;
export const ALTERNATE_TITLE = `${AAT}300417227`;
export const ENGLISH = `${AAT}300388277`;
export const FRENCH = `${AAT}300388306`;

/** A `HumanMadeObject` draft with `content` merged in. */
export function draft(content: Record<string, unknown> = {}): Y.Doc {
  return hydrate({ id: RECORD_URI, type: "HumanMadeObject", ...content });
}

/** The projected JSON-LD with internal node IRIs replaced by `"<node>"`. */
export function projected(doc: Y.Doc): Record<string, unknown> {
  const text = JSON.stringify(project(doc)).replaceAll(
    new RegExp(`"${RECORD_URI}/node/[^"]+"`, "g"),
    '"<node>"',
  );
  return JSON.parse(text) as Record<string, unknown>;
}

function term(uri: string, label: string, extra: Record<string, unknown> = {}) {
  return {
    uri,
    label: { en: label },
    default_label: label,
    is_default: false,
    deprecated: false,
    ...extra,
  };
}

/** Value Sets and bindings shaped like `GET /api/vocab/bundle`, as `profiles/vocab/` has them. */
export const BUNDLE: VocabBundle = {
  version: "test",
  vocabularies: [
    {
      id: "vocab:work_title_types",
      version: 1,
      label: { en: "Title types" },
      mode: "enumerated",
      enforcement: "extensible",
      terms: [
        term(PRIMARY_NAME, "Primary Name", { is_default: true }),
        term(ALTERNATE_TITLE, "Alternate Title"),
      ],
    },
    {
      id: "vocab:type_of_work",
      version: 1,
      label: { en: "Type of Work" },
      mode: "rule",
      enforcement: "extensible",
      rule: {
        metatype_uris: [`${AAT}300435443`],
        broader_roots: [],
        exclusions: [`${AAT}300133025`],
        classes: [],
      },
      terms: [term(`${AAT}300033618`, "painting"), term(`${AAT}300041273`, "print")],
    },
    {
      id: "vocab:style",
      version: 1,
      label: { en: "Style" },
      mode: "rule",
      enforcement: "advisory",
      rule: { metatype_uris: [`${AAT}300015646`], broader_roots: [], exclusions: [], classes: [] },
      terms: [term(`${AAT}300021140`, "Baroque")],
    },
    {
      id: "vocab:material",
      version: 1,
      label: { en: "Material" },
      mode: "rule",
      enforcement: "extensible",
      rule: { metatype_uris: [], broader_roots: [], exclusions: [], classes: ["Material"] },
      terms: [term(`${AAT}300015050`, "oil paint"), term(`${AAT}300014078`, "canvas")],
    },
  ],
  bindings: [
    ["HumanMadeObject", "identified_by[Name].classified_as", "vocab:work_title_types"],
    ["HumanMadeObject", "classified_as", "vocab:type_of_work"],
    ["HumanMadeObject", "classified_as", "vocab:style"],
    ["HumanMadeObject", "made_of", "vocab:material"],
  ].map(([entity_class = "", property_path = "", vocabulary_id = ""]) => ({
    entity_class,
    property_path,
    vocabulary_id,
    enforcement: null,
    effective_enforcement: "extensible" as const,
  })),
};

/** A slot as `GET /api/profiles/{class}` returns it. */
export function slot(fields: Partial<ProfileSlot> & Pick<ProfileSlot, "id" | "path" | "widget">) {
  return {
    label: { en: fields.id.charAt(0).toUpperCase() + fields.id.slice(1) },
    multiple: true,
    required: false,
    advanced: false,
    ...fields,
  } as ProfileSlot;
}

/** The local origin the widgets write with in tests. */
export const ORIGIN = { test: "local" };

/** Render `ui` with i18n, a query client and a {@link WidgetEnv} over `doc`. */
export function renderWidget(ui: ReactNode, doc: Y.Doc, env: Partial<WidgetEnv> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const value: WidgetEnv = {
    doc,
    origin: ORIGIN,
    disabled: false,
    recordClass: "HumanMadeObject",
    bundle: BUNDLE,
    refLabels: new Map(),
    classificationGroups: new Map(),
    ...env,
  };
  return render(
    <I18nextProvider i18n={createI18n(DEFAULT_LANGUAGE)}>
      <QueryClientProvider client={queryClient}>
        <Suspense fallback={null}>
          <WidgetEnvContext.Provider value={value}>{ui}</WidgetEnvContext.Provider>
        </Suspense>
      </QueryClientProvider>
    </I18nextProvider>,
  );
}
