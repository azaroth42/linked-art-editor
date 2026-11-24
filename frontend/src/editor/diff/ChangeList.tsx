import { useTranslation } from "react-i18next";

import type { UiProfile } from "../../api/hooks/editing.ts";
import type { Change, ChangeStep, RefSummary } from "../../api/hooks/publishing.ts";
import { isInternalNodeIri } from "../../crdt/hydrate.ts";
import { useLanguage } from "../../widgets/environment.ts";
import { localized } from "../../widgets/vocab.ts";
import { type PointerStep, slotForSteps } from "../validation/locate.ts";

interface ChangeListProps {
  changes: readonly Change[];
  /** The record's URI: a "reference" to one of its internal node IRIs is a new empty node. */
  recordUri: string;
  /** The record's UI Profile, to name each change by the field that edits it. */
  profile: UiProfile | null;
}

/** The steps from the record to what a change is about, for `slotForSteps`. */
function changeSteps(change: Change): PointerStep[] {
  const steps = change.parent.map((step: ChangeStep) => ({ key: step.property, type: step.type }));
  let type: string | null = null;
  if (change.kind === "add_node" || change.kind === "remove_node") {
    type = change.node_type;
  } else if (change.kind === "add_ref" || change.kind === "remove_ref") {
    type = change.type;
  }
  return [...steps, { key: change.property, type }];
}

function literal(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** The text a node is summarized by: its `content`, else its first plain-text field. */
function nodeText(fields: Record<string, unknown>): string | null {
  if (typeof fields.content === "string") {
    return fields.content;
  }
  for (const value of Object.values(fields)) {
    if (typeof value === "string") {
      return value;
    }
  }
  return null;
}

/**
 * The typed changes of a semantic diff (`lae.diff.semantic`, contract 1.14), one list item each,
 * worded per change kind by the `editor:diff.<kind>` messages and placed under the label of the
 * profile slot that edits them (else the change's own `path`). The Publish dialog shows the
 * draft's changes with it; the Revision History Browser (Step 1.20) shows a diff of two versions.
 *
 * An empty nested node added on its own (`{id, type}` with an internal node IRI) arrives as
 * `add_ref` / `remove_ref` whose `uri` is the node IRI (contract 1.14 §8, item 3); it is shown as
 * the node it is, never as a link.
 */
export function ChangeList({ changes, recordUri, profile }: ChangeListProps) {
  const { t } = useTranslation("editor");
  const language = useLanguage();

  const className = (type: string | null) =>
    type === null ? t("diff.unknown_class") : t(`classes.${type}`, { defaultValue: type });

  const ref = (summary: Pick<RefSummary, "uri" | "type" | "label">) =>
    t("diff.ref", { label: summary.label ?? summary.uri, class: className(summary.type) });

  const node = (type: string | null, fields: Record<string, unknown>) => {
    const text = nodeText(fields);
    return text === null
      ? className(type)
      : t("diff.node", { class: className(type), summary: text });
  };

  const describe = (change: Change): string => {
    switch (change.kind) {
      case "add_node":
        return t("diff.add_node", { node: node(change.node_type, change.summary_fields) });
      case "remove_node":
        return t("diff.remove_node", { node: node(change.node_type, change.summary_fields) });
      case "add_ref":
        return isInternalNodeIri(change.uri, recordUri)
          ? t("diff.add_node", { node: className(change.type) })
          : t("diff.add_ref", { target: ref(change) });
      case "remove_ref":
        return isInternalNodeIri(change.uri, recordUri)
          ? t("diff.remove_node", { node: className(change.type) })
          : t("diff.remove_ref", { target: ref(change) });
      case "update_literal":
        return t("diff.update_literal", {
          op: change.op,
          old: literal(change.old),
          new: literal(change.new),
        });
      case "reorder":
        return t("diff.reorder");
    }
  };

  /** The label of the slot that edits what the change is about, if a slot does. */
  const field = (change: Change): string | null => {
    // The root _label is derived from the names (Step 1.19a); no slot edits it.
    if (change.parent.length === 0 && change.property === "_label") {
      return t("diff.derived_label");
    }
    const slot = profile === null ? null : slotForSteps(profile, changeSteps(change));
    return slot === null ? null : localized(slot.label, language);
  };

  return (
    <ol className="flex flex-col gap-2" data-testid="change-list">
      {changes.map((change, index) => {
        const label = field(change);
        return (
          <li
            // Changes have no identity of their own; the list is replaced, never edited.
            key={index}
            className="flex flex-col rounded border border-slate-200 bg-slate-50 px-3 py-2"
            data-kind={change.kind}
          >
            <span className="text-slate-900">
              {t("diff.in_field", { field: label ?? change.path, change: describe(change) })}
            </span>
            {label !== null && (
              <code dir="ltr" className="text-xs break-all text-slate-700">
                {change.path}
              </code>
            )}
          </li>
        );
      })}
    </ol>
  );
}
