import { type KeyboardEvent, useEffect, useId, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  AUTOCOMPLETE_MIN_LENGTH,
  type AutocompleteResult,
  useAutocomplete,
} from "../api/hooks/editing.ts";
import { useYPath } from "../crdt/useYPath.ts";
import { CreateStubDialog, type CreatedStub } from "./CreateStubDialog.tsx";
import { type NewReference, referencesAt, removeReference, replaceReferences } from "./edits.ts";
import { useLanguage, useSlotText, useWidgetEnv, type WidgetProps } from "./environment.ts";
import { SlotFieldset } from "./SlotFieldset.tsx";
import { FIELD, HELP, INPUT, LABEL, SMALL_BUTTON } from "./styles.ts";
import { referenceLabel } from "./vocab.ts";

/** Wait this long after the last keystroke before asking the server (milliseconds). */
export const AUTOCOMPLETE_DEBOUNCE = 250;

type Option =
  { kind: "result"; result: AutocompleteResult } | { kind: "create"; recordClass: string };

function useDebounced(value: string, delay: number): string {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

/**
 * `entity_ref`: references to records of the slot's `allowed_classes`. The current references
 * are listed with a remove button each; the combobox searches `/api/search/autocomplete` with
 * those classes as `types` and shows each hit's disambiguation context. "Create new …" (one per
 * allowed class) opens {@link CreateStubDialog}, which creates a stub record, and the reference
 * to it is inserted. A new reference keeps the picked label as `_label` (publish removes it
 * again once the target has a label of its own, spec §3.2). In a single-valued slot a new
 * reference replaces the old one. Keyboard: arrows move through the options, Enter picks one,
 * Escape closes the list.
 */
export function EntityRefAutocomplete({ slot, base }: WidgetProps) {
  const { t } = useTranslation("editor");
  const env = useWidgetEnv();
  const language = useLanguage();
  const { label, help } = useSlotText(slot);
  const inputId = useId();
  const listId = useId();
  const statusId = useId();
  const classes = useMemo(() => slot.allowed_classes ?? [], [slot.allowed_classes]);
  const { json } = useYPath(env.doc, slot.path, { base });
  // `json` changes whenever the references do.
  const references = useMemo(
    () => (json.length === 0 ? [] : referencesAt(env.doc, slot.path, base)),
    [env.doc, slot.path, base, json],
  );
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [creating, setCreating] = useState<string | null>(null);
  const debounced = useDebounced(query, AUTOCOMPLETE_DEBOUNCE);
  const search = useAutocomplete(debounced, classes);

  const typed = query.trim();
  const options: Option[] = [
    ...(typed.length >= AUTOCOMPLETE_MIN_LENGTH && debounced.trim() === typed
      ? (search.data ?? []).map((result): Option => ({ kind: "result", result }))
      : []),
    ...(typed === ""
      ? []
      : classes.map((recordClass): Option => ({ kind: "create", recordClass }))),
  ];
  const expanded = open && options.length > 0;
  const optionId = (index: number) => `${listId}-${String(index)}`;
  const className = (recordClass: string) =>
    t(`classes.${recordClass}`, { defaultValue: recordClass });

  const insert = (reference: NewReference) => {
    replaceReferences(env.doc, slot.path, {
      base,
      origin: env.origin,
      remove: () => !slot.multiple,
      add: reference,
    });
    setQuery("");
    setOpen(false);
    setActive(-1);
  };

  const choose = (option: Option) => {
    if (option.kind === "result") {
      insert({ id: option.result.id, type: option.result.type, label: option.result.label });
    } else {
      setOpen(false);
      setCreating(option.recordClass);
    }
  };

  const onCreated = (stub: CreatedStub) => {
    setCreating(null);
    insert({ id: stub.uri, type: stub.type, label: stub.name });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setOpen(true);
      setActive((index) => (options.length === 0 ? -1 : (index + 1) % options.length));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setOpen(true);
      setActive((index) =>
        options.length === 0 ? -1 : (index - 1 + options.length) % options.length,
      );
    } else if (event.key === "Enter") {
      const option = expanded ? options[active] : undefined;
      if (option !== undefined) {
        event.preventDefault();
        choose(option);
      }
    } else if (event.key === "Escape" && expanded) {
      event.preventDefault();
      setOpen(false);
      setActive(-1);
    }
  };

  const remove = (key: string | null) =>
    removeReference(env.doc, slot.path, key, { base, origin: env.origin });

  let status: string;
  if (typed.length > 0 && typed.length < AUTOCOMPLETE_MIN_LENGTH) {
    status = t("widgets.entity_ref.too_short", { count: AUTOCOMPLETE_MIN_LENGTH });
  } else if (typed.length > 0 && (search.isFetching || debounced.trim() !== typed)) {
    status = t("widgets.entity_ref.searching");
  } else if (search.isError) {
    status = t("widgets.entity_ref.search_failed");
  } else if (typed.length > 0 && search.data?.length === 0) {
    status = t("widgets.entity_ref.no_results");
  } else {
    status = "";
  }

  return (
    <SlotFieldset label={label} help={help} required={slot.required} empty={false}>
      {references.length === 0 ? (
        <p className={HELP}>{t("widgets.empty")}</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {references.map(({ key, reference }) => {
            const name = referenceLabel(reference, env.bundle, env.refLabels, language);
            return (
              <li key={key ?? reference.id} className="flex flex-wrap items-center gap-2">
                <span className="text-slate-900">{name}</span>
                {reference.type !== null && (
                  <span className="text-sm text-slate-700">
                    {t("widgets.entity_ref.class", { class: className(reference.type) })}
                  </span>
                )}
                <button
                  type="button"
                  className={SMALL_BUTTON}
                  disabled={env.disabled}
                  onClick={() => remove(key)}
                >
                  {t("widgets.remove", { name })}
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <div className={`${FIELD} relative`}>
        <label htmlFor={inputId} className={LABEL}>
          {t("widgets.entity_ref.search", { label })}
        </label>
        <input
          id={inputId}
          type="text"
          role="combobox"
          className={INPUT}
          value={query}
          disabled={env.disabled}
          autoComplete="off"
          aria-autocomplete="list"
          aria-expanded={expanded}
          aria-controls={listId}
          aria-activedescendant={expanded && active >= 0 ? optionId(active) : undefined}
          aria-describedby={status === "" ? undefined : statusId}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
            setActive(-1);
          }}
          onKeyDown={onKeyDown}
          onBlur={() => setOpen(false)}
        />
        {status !== "" && (
          <p id={statusId} className={HELP}>
            {status}
          </p>
        )}
        <ul
          id={listId}
          role="listbox"
          aria-label={t("widgets.entity_ref.options", { label })}
          hidden={!expanded}
          className="absolute inset-x-0 top-full z-10 mt-1 max-h-72 overflow-auto rounded border border-slate-400 bg-white shadow-lg"
        >
          {options.map((option, index) => (
            // The input owns the keyboard (arrows, Enter, Escape through aria-activedescendant).
            // eslint-disable-next-line jsx-a11y/click-events-have-key-events
            <li
              key={option.kind === "result" ? option.result.id : `create-${option.recordClass}`}
              id={optionId(index)}
              role="option"
              aria-selected={index === active}
              className="min-h-6 cursor-pointer px-3 py-1 text-slate-900 aria-selected:bg-blue-100"
              // Keep the focus in the input so the list does not close before the click.
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => choose(option)}
            >
              {option.kind === "result" ? (
                <span className="flex flex-col">
                  <span>{option.result.label}</span>
                  <span className="text-sm text-slate-700">
                    {option.result.context === null
                      ? className(option.result.type)
                      : t("widgets.entity_ref.context", {
                          class: className(option.result.type),
                          context: option.result.context,
                        })}
                  </span>
                </span>
              ) : (
                t("widgets.entity_ref.create", {
                  class: className(option.recordClass),
                  name: typed,
                })
              )}
            </li>
          ))}
        </ul>
      </div>
      {creating !== null && (
        <CreateStubDialog
          recordClass={creating}
          initialName={typed}
          onCreated={onCreated}
          onCancel={() => setCreating(null)}
        />
      )}
    </SlotFieldset>
  );
}
