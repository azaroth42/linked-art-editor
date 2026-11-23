/**
 * What every widget needs besides its slot: the live document, the local origin to pass to every
 * `paths.ts` write (so that the scoped `Y.UndoManager` tracks it), whether editing is allowed,
 * the Value Set bundle, and labels. `ProfileRenderer` provides it; widget tests provide their own.
 */
import { createContext, useContext, useMemo } from "react";
import { useTranslation } from "react-i18next";
import type * as Y from "yjs";

import type { ProfileSlot, VocabBundle } from "../api/hooks/editing.ts";
import { listEntries, type YMap } from "../crdt/paths.ts";
import { useYPath } from "../crdt/useYPath.ts";
import type { ClassificationGroup } from "./classifications.ts";
import { localized } from "./vocab.ts";

export interface WidgetEnv {
  doc: Y.Doc;
  /** The transaction origin of this editor's own changes (`RecordSession.localOrigin`). */
  origin: object;
  /** No writes: not connected yet, read-only, or without `edit_draft`. */
  disabled: boolean;
  /** The record's class (bindings are looked up for it). */
  recordClass: string;
  bundle: VocabBundle | null;
  /** Labels the server transcluded on the record's references when it was loaded, by URI. */
  refLabels: ReadonlyMap<string, string>;
  /** The cards of each path that `classification` slots edit, by path. */
  classificationGroups: ReadonlyMap<string, ClassificationGroup>;
}

export const WidgetEnvContext = createContext<WidgetEnv | null>(null);

/** The {@link WidgetEnv} of the enclosing editor. */
export function useWidgetEnv(): WidgetEnv {
  const env = useContext(WidgetEnvContext);
  if (env === null) {
    throw new Error("useWidgetEnv outside a WidgetEnvContext provider");
  }
  return env;
}

/** Props of every widget: its slot, and the object map its path starts from (`null`: root). */
export interface WidgetProps {
  slot: ProfileSlot;
  base: YMap | null;
}

/** The array entries `path` addresses from `base`, as `[entry key, map]`, kept current. */
export function useEntries(doc: Y.Doc, path: string, base: YMap | null): [string, YMap][] {
  const { values } = useYPath(doc, path, { base });
  // `values` changes identity whenever what the path addresses changes.
  return useMemo(
    () => (values.length === 0 ? [] : listEntries(doc, path, { base })),
    [doc, path, base, values],
  );
}

/** The JSON of the single value `path` addresses from `base` (`undefined` if none), kept current. */
export function useValue(doc: Y.Doc, path: string, base: YMap | null): unknown {
  return useYPath(doc, path, { base }).json[0];
}

/** The slot's label and help text in the UI language. */
export function useSlotText(slot: ProfileSlot): { label: string; help: string } {
  const { i18n } = useTranslation();
  const language = i18n.resolvedLanguage ?? i18n.language;
  return {
    label: localized(slot.label, language),
    help: localized(slot.help_text ?? null, language),
  };
}

/** The active UI language (for LangMaps and `Intl`). */
export function useLanguage(): string {
  const { i18n } = useTranslation();
  return i18n.resolvedLanguage ?? i18n.language;
}
