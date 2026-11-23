/**
 * Parser for UI Profile paths; the grammar is specified in `profiles/README.md` ("Path syntax").
 * TypeScript mirror of `backend/src/lae/profiles/paths.py`.
 */

/** Same pattern as `$defs.Path` in `profiles/{ui,vocab}/_schema.json` and `PATH_PATTERN`. */
export const PATH_PATTERN =
  /^[a-z][a-z0-9_]*(?:\[[A-Z][A-Za-z]*\])?(?:\.[a-z][a-z0-9_]*(?:\[[A-Z][A-Za-z]*\])?)*$/;

const SEGMENT = /^([a-z][a-z0-9_]*)(?:\[([A-Z][A-Za-z]*)\])?$/;

/** Thrown when a string is not a valid UI Profile path. */
export class PathSyntaxError extends Error {
  constructor(path: string) {
    super(`not a valid profile path: ${JSON.stringify(path)}`);
    this.name = "PathSyntaxError";
  }
}

/** One step of a profile path: a JSON-LD key and an optional `type` filter. */
export interface PathSegment {
  readonly key: string;
  readonly typeFilter: string | null;
}

/** Render a segment in path syntax, e.g. `identified_by[Name]`. */
export function formatSegment(segment: PathSegment): string {
  return segment.typeFilter === null ? segment.key : `${segment.key}[${segment.typeFilter}]`;
}

/** Split a profile path into its segments; throws {@link PathSyntaxError} if malformed. */
export function parsePath(path: string): PathSegment[] {
  if (!PATH_PATTERN.test(path)) {
    throw new PathSyntaxError(path);
  }
  return path.split(".").map((part) => {
    const match = SEGMENT.exec(part);
    if (match === null) {
      throw new PathSyntaxError(path); // unreachable once PATH_PATTERN matched
    }
    return { key: match[1] as string, typeFilter: match[2] ?? null };
  });
}

/** Join segments back into path syntax (inverse of {@link parsePath}). */
export function formatPath(segments: readonly PathSegment[]): string {
  return segments.map(formatSegment).join(".");
}
