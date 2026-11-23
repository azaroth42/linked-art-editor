/**
 * Which `classification` card shows which value (decisions C4, C5; `profiles/README.md`).
 *
 * Several Value Sets may govern one path of a class (Type of Work and Style both live in a
 * `HumanMadeObject`'s `classified_as`; Occupation, Nationality and Gender in a `Person`'s). Each
 * is its own card: first the profile's `classification` slots on that path, in form order, then
 * the other Value Sets the bundle binds to `(class, path)`. A value is shown on the first card
 * whose Value Set **claims** it, and a value no card claims goes to "Other classifications",
 * where it can only be removed: nothing is ever dropped.
 *
 * A Value Set claims a reference when its URI is one of the set's terms, or, for a rule set, when
 * the reference carries one of the rule's metatypes inline (`classified_as`, ADR-0032) or, for a
 * rule with neither metatypes nor broader roots, when the reference's class is one of the rule's
 * `classes`. The rule's `exclusions` are never claimed. The full rule membership is resolved on
 * the server (`vocabulary_membership`); the bundle carries what the client can decide alone.
 */
import type { BundleVocabulary, UiProfile, VocabBundle } from "../api/hooks/editing.ts";
import { boundVocabularyIds, findVocabulary, type Reference } from "./vocab.ts";

/** Whether `vocabulary` claims `reference` (see the module comment). */
export function claims(vocabulary: BundleVocabulary, reference: Reference): boolean {
  const rule = vocabulary.rule ?? null;
  if (rule?.exclusions.includes(reference.id)) {
    return false;
  }
  if (vocabulary.terms.some((term) => term.uri === reference.id)) {
    return true;
  }
  if (vocabulary.mode !== "rule" || rule === null) {
    return false;
  }
  if (reference.metatypes.some((uri) => rule.metatype_uris.includes(uri))) {
    return true;
  }
  return (
    rule.metatype_uris.length === 0 &&
    rule.broader_roots.length === 0 &&
    reference.type !== null &&
    rule.classes.includes(reference.type)
  );
}

/** The values of one path split by card. */
export interface Partition<T extends Reference> {
  /** Values by the id of the Value Set whose card shows them. */
  claimed: Map<string, T[]>;
  /** Values no card claims ("Other classifications"). */
  other: T[];
}

/** Put each value on the first card (in `vocabularies` order) that claims it. */
export function partitionClassifications<T extends Reference>(
  values: readonly T[],
  vocabularies: readonly BundleVocabulary[],
): Partition<T> {
  const claimed = new Map<string, T[]>(vocabularies.map((vocabulary) => [vocabulary.id, []]));
  const other: T[] = [];
  for (const value of values) {
    const owner = vocabularies.find((vocabulary) => claims(vocabulary, value));
    if (owner === undefined) {
      other.push(value);
    } else {
      claimed.get(owner.id)?.push(value);
    }
  }
  return { claimed, other };
}

/** The cards of one path. */
export interface ClassificationGroup {
  /** The Value Sets of the cards, in card order. */
  vocabularies: BundleVocabulary[];
  /** Value Sets bound to the path without a slot of their own: the owner renders their cards. */
  unslotted: BundleVocabulary[];
  /** The slot that renders the unslotted cards and "Other classifications": the path's last. */
  ownerSlotId: string;
}

/** The {@link ClassificationGroup} of every path that has a `classification` slot, by path. */
export function classificationGroups(
  profile: UiProfile,
  bundle: VocabBundle | null,
): Map<string, ClassificationGroup> {
  const slotsByPath = new Map<string, { ids: string[]; owner: string }>();
  for (const section of profile.sections) {
    for (const slot of section.slots) {
      if (slot.widget !== "classification" || slot.value_set === undefined) {
        continue;
      }
      const entry = slotsByPath.get(slot.path) ?? { ids: [], owner: slot.id };
      if (slot.value_set !== null && !entry.ids.includes(slot.value_set)) {
        entry.ids.push(slot.value_set);
      }
      entry.owner = slot.id;
      slotsByPath.set(slot.path, entry);
    }
  }
  const groups = new Map<string, ClassificationGroup>();
  for (const [path, { ids, owner }] of slotsByPath) {
    const extra = boundVocabularyIds(bundle, profile.class, path).filter((id) => !ids.includes(id));
    const resolve = (list: string[]) =>
      list
        .map((id) => findVocabulary(bundle, id))
        .filter((vocabulary): vocabulary is BundleVocabulary => vocabulary !== null);
    const unslotted = resolve(extra);
    groups.set(path, {
      vocabularies: [...resolve(ids), ...unslotted],
      unslotted,
      ownerSlotId: owner,
    });
  }
  return groups;
}
