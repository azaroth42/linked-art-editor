#!/usr/bin/env python3
# ruff: noqa: E501  (report generator: long Markdown strings are clearer unwrapped)
"""Report every mismatch between the golden corpus and the vendored Linked Art JSON Schema.

Writes ``docs/upstream/linked-art-schema-mismatches.md``: a checklist for fixing the schema
(``linked-art/json-validator``) or the examples (``linked-art/linked.art``) upstream. Every
error is listed (not truncated), with the JSON Pointer into the record, the failing keyword, the
schema definition responsible (the last ``$ref`` followed), and for linked.art examples the
model page and line whose ``crom`` block generates the example.

Run from the repository root after an upstream fix (re-vendor the schema or re-sync the corpus
first):

    uv run --project backend python scripts/report_schema_mismatches.py \
        [--linkedart /path/to/linked.art] [--validator /path/to/json-validator]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from jsonschema.exceptions import ValidationError, best_match
from referencing.exceptions import Unresolvable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend" / "src"))
from lae.rdf import strip_embedded_ids, strip_nested_contexts  # noqa: E402
from lae.validation.schema_map import (  # noqa: E402
    SCHEMA_BASE_URI,
    load_registry,
    schema_name_for,
    validator_for,
)

CORPUS = REPO / "tests" / "corpus"
OUT = REPO / "docs" / "upstream" / "linked-art-schema-mismatches.md"

CATEGORIES = {
    "schema-defect": ("Schema defect (unresolvable `$ref`)", "json-validator"),
    "missing-label": ("Top-level `_label` missing", "examples (or relax schema)"),
    "reference-without-id": (
        "Reference without `id`",
        "decide: schema allows unidentified stubs, or examples add ids",
    ),
    "cardinality": ("Array vs single object", "decide per property; fix the other side"),
    "property-not-allowed": (
        "Property or shape not allowed by schema",
        "usually schema (model has moved on)",
    ),
    "enum-or-const": ("Value not in allowed set", "decide per case"),
    "other": ("Other", "inspect"),
}


def git_rev(path: Path) -> str:
    try:
        rev = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return rev + (" (with uncommitted changes)" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def ignored_ids_of(raw: Any, checked: Any, path: tuple[Any, ...] = ()) -> list[tuple[str, str]]:
    """``(pointer, type)`` of each node whose ``id`` ``strip_embedded_ids`` removed."""
    found: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for position, (before, after) in enumerate(zip(raw, checked, strict=True)):
            found += ignored_ids_of(before, after, (*path, position))
    elif isinstance(raw, dict):
        if "id" in raw and "id" not in checked:
            found.append((pointer(path), str(raw.get("type"))))
        for key, value in raw.items():
            if key in checked:
                found += ignored_ids_of(value, checked[key], (*path, key))
    return found


def schema_location(error: ValidationError, registry: Any, root_name: str) -> str:
    """Exact location of the failing keyword: ``file.json#/json/pointer`` (through ``$ref``s)."""
    resolver = registry.resolver(SCHEMA_BASE_URI + root_name)
    node: Any = registry[SCHEMA_BASE_URI + root_name].contents
    base, tail = root_name + "#", []
    for step in error.absolute_schema_path:
        if step == "$ref" and isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            resolved = resolver.lookup(ref)
            resolver, node = resolved.resolver, resolved.contents
            file_part, _, frag = ref.partition("#")
            file_part = file_part.removeprefix(SCHEMA_BASE_URI) or base.split("#", 1)[0]
            base, tail = f"{file_part}#{frag}", []
            continue
        if isinstance(node, dict) and step in node:
            node = node[step]
        elif isinstance(node, list) and isinstance(step, int) and step < len(node):
            node = node[step]
        tail.append(str(step))
    return base + ("/" + "/".join(tail) if tail else "")


def leaf(error: ValidationError) -> ValidationError:
    """For anyOf/oneOf, descend to the most relevant alternative's error."""
    while error.context:
        error = best_match(error.context)
    return error


def categorize(error: ValidationError) -> str:
    e = leaf(error)
    msg = e.message
    if e.validator == "required" and "'_label'" in msg and not list(e.absolute_path):
        return "missing-label"
    if e.validator == "required" and "'id'" in msg:
        return "reference-without-id"
    if e.validator == "type" and ("'array'" in msg or "'object'" in msg):
        return "cardinality"
    if e.validator in ("additionalProperties", "unevaluatedProperties"):
        return "property-not-allowed"
    if e.validator in ("enum", "const"):
        return "enum-or-const"
    if e.validator in ("anyOf", "oneOf"):
        return "property-not-allowed"
    return "other"


def pointer(path: Any) -> str:
    return "/" + "/".join(str(p) for p in path)


def short(text: str, n: int = 220) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def crom_blocks(linkedart: Path) -> list[tuple[str, str]]:
    """Every ```crom block in the linked.art docs as ``(page:line, block text)``."""
    blocks: list[tuple[str, str]] = []
    for md in sorted((linkedart / "docs").rglob("*.md")):
        lines = md.read_text(errors="replace").splitlines()
        i = 0
        while i < len(lines):
            if lines[i].strip().startswith("```crom"):
                start = i + 1
                j = start
                while j < len(lines) and not lines[j].strip().startswith("```"):
                    j += 1
                blocks.append(
                    (f"{md.relative_to(linkedart).as_posix()}:{start}", "\n".join(lines[start:j]))
                )
                i = j
            i += 1
    return blocks


def _strings(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("content", "_label") and isinstance(v, str) and len(v) > 3:
                out.add(v)
            elif k != "@context":
                _strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _strings(v, out)


def find_page(record: dict[str, Any], ident: str | None, blocks: list[tuple[str, str]]) -> str:
    """Best guess at the crom block that generates an example."""
    if ident:
        hits = [loc for loc, text in blocks if f'ident="{ident}"' in text]
        if hits:
            return ", ".join(f"`{h}`" for h in hits)
    wanted: set[str] = set()
    _strings(record, wanted)
    wanted = {w for w in wanted if not w.startswith("http")}
    if not wanted:
        return "page not found"
    scored = []
    for loc, text in blocks:
        score = sum(1 for w in wanted if w in text)
        if score:
            scored.append((score, loc))
    if not scored:
        return "page not found"
    scored.sort(reverse=True)
    best = scored[0][0]
    tops = [loc for sc, loc in scored if sc == best][:3]
    return ", ".join(f"`{t}`" for t in tops) + f" (best match on {best}/{len(wanted)} strings)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--linkedart", type=Path, default=Path("/Users/rs2668/Development/linked-art/linked.art")
    )
    ap.add_argument(
        "--validator",
        type=Path,
        default=Path("/Users/rs2668/Development/linked-art/json-validator"),
    )
    args = ap.parse_args()

    registry = load_registry()
    blocks = crom_blocks(args.linkedart) if args.linkedart.exists() else []
    example_root = args.linkedart / "docs" / "example"
    by_bytes = (
        {p.read_bytes(): p.relative_to(args.linkedart) for p in example_root.rglob("*.json")}
        if example_root.exists()
        else {}
    )

    rows: list[dict[str, Any]] = []
    ignored_ids: list[tuple[str, str, str]] = []
    for path in sorted((CORPUS / "linkedart").glob("*.json")) + sorted(
        (CORPUS / "institutional").glob("*.json")
    ):
        raw = path.read_bytes()
        as_stored = json.loads(raw)
        # As ``lae.validation.validate_schema`` checks it: an ``id`` on an embedded node is
        # ignored, and so is a nested repeat of the pinned ``@context`` (ADR-0028). What the
        # first hides from the lists below is reported on its own.
        record = strip_embedded_ids(strip_nested_contexts(as_stored), as_stored.get("id", ""))
        schema = schema_name_for(record)
        validator = validator_for(schema, registry)
        for where, node_type in ignored_ids_of(as_stored, record):
            ignored_ids.append((path.relative_to(REPO).as_posix(), where, node_type))
        errors: list[dict[str, str]] = []
        try:
            found = sorted(validator.iter_errors(record), key=lambda e: list(e.absolute_path))
        except Unresolvable as exc:
            errors.append(
                {
                    "pointer": "(schema)",
                    "keyword": "$ref",
                    "category": "schema-defect",
                    "message": f"unresolvable $ref: {exc.ref}",
                    "where": schema,
                    "alt": "",
                }
            )
            found = []
        for err in found:
            lf = leaf(err)
            alt = ""
            if lf is not err:
                alt = f"closest alternative: `{pointer(lf.absolute_path)}` {lf.validator}: {short(lf.message, 160)}"
            errors.append(
                {
                    "pointer": pointer(err.absolute_path),
                    "keyword": err.validator,
                    "category": categorize(err),
                    "message": (
                        f"matches none of the {len(err.validator_value)} allowed shapes"
                        if err.validator in ("anyOf", "oneOf")
                        else short(err.message)
                    ),
                    "where": schema_location(lf, registry, schema),
                    "alt": alt,
                }
            )
        if not errors:
            continue
        src = by_bytes.get(raw)
        ident = None
        if src is not None:
            parts = src.with_suffix("").parts  # docs/example/<category>/<sub>/<n>
            ident = "/".join(parts[3:]) if len(parts) > 4 else None
        rows.append(
            {
                "corpus": path.relative_to(REPO).as_posix(),
                "source": src.as_posix() if src else None,
                "pages": find_page(record, ident, blocks) if blocks else "page not found",
                "type": record.get("type"),
                "schema": schema,
                "errors": errors,
                "kind": "linkedart" if path.parent.name == "linkedart" else "institutional",
                "categories": Counter(e["category"] for e in errors),
            }
        )

    upstream = [r for r in rows if r["kind"] == "linkedart"]
    inst = [r for r in rows if r["kind"] == "institutional"]
    cat_files: Counter[str] = Counter()
    cat_errors: Counter[str] = Counter()
    for r in upstream:
        for c, n in r["categories"].items():
            cat_files[c] += 1
            cat_errors[c] += n

    out: list[str] = []
    w = out.append
    w("# Linked Art JSON Schema vs. example mismatches")
    w("")
    w(
        f"Generated {dt.date.today().isoformat()} by `scripts/report_schema_mismatches.py`. Inputs: "
        f"linked.art `{git_rev(args.linkedart)}`, json-validator `{git_rev(args.validator)}`, vendored schema in "
        "`profiles/schema/`, corpus in `tests/corpus/`. Re-vendor the schema (`profiles/schema/`) or re-sync the "
        "corpus (`python3 scripts/sync_corpus.py`) after an upstream fix, then rerun:"
    )
    w("")
    w("```\nuv run --project backend python scripts/report_schema_mismatches.py\n```")
    w("")
    w(
        f"**{len(upstream)} linked.art examples** and **{len(inst)} institutional records** fail. Each linked.art entry "
        "names the example file, the model page and line of the `crom` block that generates it, and every error. "
        '"Schema location" is the definition that rejected the value (the last `$ref` followed). For `anyOf` '
        "failures the closest alternative is shown, because that is usually the real problem."
    )
    w("")
    w(
        "When an upstream fix lands, remove the file's row from `tests/corpus/KNOWN_FAILURES.md`; the corpus test "
        "fails on any listed file that starts to validate, so the list stays honest."
    )
    w("")
    w("## Summary by cause (linked.art examples)")
    w("")
    w("| Cause | Files | Errors | Likely fix side |")
    w("| :-- | --: | --: | :-- |")
    for key, (label, side) in CATEGORIES.items():
        if cat_files[key]:
            w(f"| {label} | {cat_files[key]} | {cat_errors[key]} | {side} |")
    w("")
    w(
        "A file can appear under several causes. The checklist below groups each file under its most frequent cause."
    )
    w("")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in upstream:
        groups[r["categories"].most_common(1)[0][0]].append(r)
    for key, (label, side) in CATEGORIES.items():
        if not groups.get(key):
            continue
        w(f"## {label} — {len(groups[key])} file(s)")
        w("")
        w(f"Likely fix side: {side}.")
        w("")
        for r in groups[key]:
            pages = r["pages"]
            src = f"`{r['source']}`" if r["source"] else f"`{r['corpus']}`"
            w(
                f"- [ ] {src} — `{r['type']}` → `{r['schema']}` — {len(r['errors'])} error(s) — generated by {pages}"
            )
            for e in r["errors"]:
                line = f"  - `{e['pointer']}` **{e['keyword']}** ({e['category']}): {e['message']} — schema: `{e['where']}`"
                w(line)
                if e["alt"]:
                    w(f"    - {e['alt']}")
        w("")
    if inst:
        w("## Institutional records (data-provider quirks, not linked.art upstream)")
        w("")
        w(
            "These come from public YCBA, YUAG, and Yale Library feeds (`tests/corpus/SOURCE.md`). They are listed for "
            "completeness; fix them with the data providers or accept them as known failures."
        )
        w("")
        for r in inst:
            w(
                f"- [ ] `{r['corpus']}` — `{r['type']}` → `{r['schema']}` — {len(r['errors'])} error(s)"
            )
            for e in r["errors"]:
                w(
                    f"  - `{e['pointer']}` **{e['keyword']}** ({e['category']}): {e['message']} — schema: `{e['where']}`"
                )
                if e["alt"]:
                    w(f"    - {e['alt']}")
        w("")
    if ignored_ids:
        w("## `id` on embedded nodes (not counted above)")
        w("")
        w(
            "Laertes ignores the `id` of an embedded node when it checks the schema (ADR-0028), so an error that "
            "such an `id` causes is not in the lists and counts above. The schema allows no `id` on a `Name`, "
            "`Identifier`, `TimeSpan` or `Dimension`; where an example gives one, that is still a mismatch "
            "upstream: either the schema allows `id` there, or the example drops it. Every embedded node "
            "with an `id` of its own in the corpus:"
        )
        w("")
        for corpus_path, where, node_type in ignored_ids:
            w(f"- [ ] `{corpus_path}` — `{where}` (`{node_type}`)")
        w("")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out))
    print(
        f"wrote {OUT.relative_to(REPO)}: {len(upstream)} linked.art, {len(inst)} institutional, "
        f"{sum(len(r['errors']) for r in rows)} errors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
