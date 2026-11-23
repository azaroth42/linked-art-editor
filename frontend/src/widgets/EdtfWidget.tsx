import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import type * as Y from "yjs";

import {
  addNode,
  listEntries,
  removeEntry,
  removeValue,
  setScalar,
  valueToJson,
  type YMap,
} from "../crdt/paths.ts";
import { useFormatters } from "../i18n/useFormatters.ts";
import { useSlotText, useValue, useWidgetEnv, type WidgetProps } from "./environment.ts";
import { EDTF_TYPE, parseEdtf, parseInstant } from "./edtfBounds.ts";
import { ERROR, FIELD, HELP, INPUT, LABEL } from "./styles.ts";
import { readReference } from "./vocab.ts";

/** Bounds the widget replaces: the EDTF text describes the whole span. */
const BOUNDS = ["begin_of_the_begin", "end_of_the_begin", "begin_of_the_end", "end_of_the_end"];

/** The `TimeSpan`'s name classified as EDTF, as `[entry key, map]`, or `null`. */
function edtfName(doc: Y.Doc, namesPath: string, base: YMap | null): [string, YMap] | null {
  for (const [key, map] of listEntries(doc, namesPath, { base })) {
    const types = valueToJson(map) as { classified_as?: unknown };
    const list = Array.isArray(types.classified_as) ? types.classified_as : [];
    if (list.some((entry) => readReference(entry)?.id === EDTF_TYPE)) {
      return [key, map];
    }
  }
  return null;
}

/**
 * `edtf`: the `TimeSpan` the slot path addresses, edited as one EDTF string. A valid string sets
 * `begin_of_the_begin` and `end_of_the_end` to its outer bounds (an open end has none; the two
 * inner bounds are removed) and is kept as the span's display name, a `Name` classified as EDTF
 * (`http://id.loc.gov/datatypes/edtf`), all in one transaction. A string edtf.js rejects is shown
 * with an inline error and not written. The stored bounds are shown in the UI's locale.
 */
export function EdtfWidget({ slot, base }: WidgetProps) {
  const { t } = useTranslation("editor");
  const env = useWidgetEnv();
  const { formatDate } = useFormatters();
  const id = useId();
  const helpId = useId();
  const errorId = useId();
  const boundsId = useId();
  const { label, help } = useSlotText(slot);
  const namesPath = `${slot.path}.identified_by[Name]`;
  const names = useValue(env.doc, namesPath, base);
  const begin = parseInstant(useValue(env.doc, `${slot.path}.begin_of_the_begin`, base));
  const end = parseInstant(useValue(env.doc, `${slot.path}.end_of_the_end`, base));
  // Re-read the EDTF name whenever the names change (`names` is the first one's JSON).
  const storedEntry = names === undefined ? null : edtfName(env.doc, namesPath, base);
  const storedContent = storedEntry === null ? "" : valueToJson(storedEntry[1].get("content"));
  const stored = typeof storedContent === "string" ? storedContent : "";
  const [text, setText] = useState(stored);
  useEffect(() => setText(stored), [stored]);

  const trimmed = text.trim();
  const result = trimmed === "" ? null : parseEdtf(trimmed);
  const error = result !== null && !result.ok ? result.error : null;

  const write = (next: string) => {
    const parsed = next === "" ? null : parseEdtf(next);
    if (parsed !== null && !parsed.ok) {
      return;
    }
    const { doc, origin } = env;
    doc.transact(() => {
      for (const bound of BOUNDS) {
        const value =
          parsed === null
            ? null
            : bound === "begin_of_the_begin"
              ? parsed.begin
              : bound === "end_of_the_end"
                ? parsed.end
                : null;
        if (value === null) {
          removeValue(doc, `${slot.path}.${bound}`, { base, origin });
        } else {
          setScalar(doc, `${slot.path}.${bound}`, value, { base, origin });
        }
      }
      const entry = edtfName(doc, namesPath, base);
      if (next === "") {
        if (entry !== null) {
          removeEntry(doc, namesPath, entry[0], { base, origin });
        }
      } else if (entry === null) {
        addNode(
          doc,
          namesPath,
          { content: next, classified_as: [{ id: EDTF_TYPE, type: "Type" }] },
          { base, origin },
        );
      } else {
        setScalar(doc, "content", next, { base: entry[1], origin });
      }
    }, origin);
  };

  // The era is named only before year 1 (1 BCE is astronomical year 0).
  const format = (date: Date) =>
    formatDate(date, {
      year: "numeric",
      month: "short",
      day: "numeric",
      timeZone: "UTC",
      ...(date.getUTCFullYear() <= 0 ? { era: "short" } : {}),
    });
  let bounds: string;
  if (begin === null && end === null) {
    bounds = t("widgets.edtf.no_bounds");
  } else {
    bounds = t("widgets.edtf.bounds", {
      begin: begin === null ? t("widgets.edtf.open") : format(begin),
      end: end === null ? t("widgets.edtf.open") : format(end),
    });
  }

  const described = [help === "" ? null : helpId, error === null ? null : errorId, boundsId]
    .filter((value) => value !== null)
    .join(" ");

  return (
    <div className={FIELD}>
      <label htmlFor={id} className={LABEL}>
        {slot.required ? t("widgets.required_label", { label }) : label}
      </label>
      <input
        id={id}
        type="text"
        className={INPUT}
        value={text}
        disabled={env.disabled}
        spellCheck={false}
        aria-invalid={error !== null}
        aria-describedby={described}
        onChange={(event) => {
          setText(event.target.value);
          write(event.target.value.trim());
        }}
      />
      {help !== "" && (
        <p id={helpId} className={HELP}>
          {help}
        </p>
      )}
      {error !== null && (
        <p id={errorId} className={ERROR}>
          {t(`widgets.edtf.${error}`)}
        </p>
      )}
      <p id={boundsId} className={HELP} data-testid={`${slot.id}-bounds`}>
        {bounds}
      </p>
    </div>
  );
}
