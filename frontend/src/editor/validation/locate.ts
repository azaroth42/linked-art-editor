/**
 * From a diagnostic to the form field that edits what it is about (Step 1.19).
 *
 * A diagnostic's `path` is a JSON Pointer into the projected draft it was found in
 * (`/identified_by/0/content`); a slot's `path` is a UI Profile path (`identified_by[Name]`,
 * `profiles/README.md`, "Path syntax", parsed by `crdt/pathSyntax.ts`). The pointer is walked
 * through that document: each property becomes a step `{key, type}`, where `type` is the class of
 * the object the step reaches (the array item, when the pointer goes on into one). A slot matches
 * when its segments agree with the steps, key by key, a class filter with the step's class.
 *
 * - The slot that contains what the pointer names wins, the most specific first
 *   (`produced_by.timespan` for `/produced_by/timespan/begin_of_the_begin`).
 * - Otherwise a slot below what the pointer names (`/identified_by` → `identified_by[Name]`).
 * - Ties go to the slot that comes first in the profile.
 * - A `LAE-SCHEMA-required` diagnostic is about the missing property: it is looked up as a step
 *   below its path first (`missing: ["timespan"]` at `/produced_by`).
 * - Nothing matches the record itself (`""`), or a property no slot edits, except a missing
 *   root `_label`: it is derived from the names (Step 1.19a), so it goes to the first slot whose
 *   path is `identified_by[Name]` (`isMissingRootLabel`).
 */
import type { ProfileSlot, UiProfile } from "../../api/hooks/editing.ts";
import type { Diagnostic } from "../../api/hooks/publishing.ts";
import { parsePath, type PathSegment, PathSyntaxError } from "../../crdt/pathSyntax.ts";
import { isObject } from "../../crdt/rules.ts";
import { isMissingRootLabel, NAME_SLOT_PATH } from "./messages.ts";

/** One property on the way from the record to what a pointer names. */
export interface PointerStep {
  key: string;
  /** The class of the object reached (`null`: not an object, an array, or no `type`). */
  type: string | null;
}

/** The reference tokens of a JSON Pointer (RFC 6901), unescaped; `[]` for the root. */
export function pointerTokens(pointer: string): string[] {
  if (pointer === "") {
    return [];
  }
  return pointer
    .slice(1)
    .split("/")
    .map((token) => token.replaceAll("~1", "/").replaceAll("~0", "~"));
}

function classOf(value: unknown): string | null {
  return isObject(value) && typeof value.type === "string" ? value.type : null;
}

/** The property steps of `pointer` through `document`; array indexes refine the last step. */
export function pointerSteps(document: unknown, pointer: string): PointerStep[] {
  const steps: PointerStep[] = [];
  let current: unknown = document;
  for (const token of pointerTokens(pointer)) {
    const index = /^(0|[1-9]\d*)$/.test(token);
    // Below a value that is not there (any more), a number is still an index: properties are
    // never numbers.
    if (Array.isArray(current) || (current === undefined && index && steps.length > 0)) {
      current = Array.isArray(current) && index ? (current as unknown[])[Number(token)] : undefined;
      const last = steps[steps.length - 1];
      if (last !== undefined) {
        last.type = classOf(current);
      }
    } else {
      current = isObject(current) ? current[token] : undefined;
      steps.push({ key: token, type: classOf(current) });
    }
  }
  return steps;
}

function agrees(segment: PathSegment, step: PointerStep): boolean {
  return (
    segment.key === step.key &&
    (segment.typeFilter === null || step.type === null || segment.typeFilter === step.type)
  );
}

/** How well a slot fits the steps: `0` not at all; containing beats contained; longer wins. */
function score(segments: PathSegment[], steps: PointerStep[]): number {
  const shared = Math.min(segments.length, steps.length);
  if (shared === 0) {
    return 0;
  }
  const fits = steps
    .slice(0, shared)
    .every((step, index) => segments[index] !== undefined && agrees(segments[index], step));
  if (!fits) {
    return 0;
  }
  return segments.length <= steps.length ? 2 * segments.length : 2 * steps.length - 1;
}

function segmentsOf(slot: ProfileSlot): PathSegment[] | null {
  try {
    return parsePath(slot.path);
  } catch (error) {
    if (error instanceof PathSyntaxError) {
      return null;
    }
    throw error;
  }
}

/** Every slot of a profile, in profile order. */
export function profileSlots(profile: UiProfile): ProfileSlot[] {
  return profile.sections.flatMap((section) => section.slots);
}

/** The slot that best fits `steps`, or `null`. */
export function slotForSteps(profile: UiProfile, steps: PointerStep[]): ProfileSlot | null {
  let best: ProfileSlot | null = null;
  let bestScore = 0;
  for (const slot of profileSlots(profile)) {
    const segments = segmentsOf(slot);
    const fit = segments === null ? 0 : score(segments, steps);
    if (fit > bestScore) {
      best = slot;
      bestScore = fit;
    }
  }
  return best;
}

/** The slot of the field a diagnostic found in `document` is about, or `null`. */
export function slotForDiagnostic(
  profile: UiProfile,
  document: unknown,
  diagnostic: Pick<Diagnostic, "code" | "path" | "params">,
): ProfileSlot | null {
  if (isMissingRootLabel(diagnostic)) {
    return profileSlots(profile).find((slot) => slot.path === NAME_SLOT_PATH) ?? null;
  }
  const steps = pointerSteps(document, diagnostic.path);
  const missing = diagnostic.params.missing;
  if (diagnostic.code === "LAE-SCHEMA-required" && Array.isArray(missing)) {
    for (const key of missing) {
      if (typeof key === "string") {
        const found = slotForSteps(profile, [...steps, { key, type: null }]);
        if (found !== null) {
          return found;
        }
      }
    }
  }
  return slotForSteps(profile, steps);
}
