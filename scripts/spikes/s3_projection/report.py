"""Print the corpus statistics quoted in ``docs/spikes/S3-projection.md``.

cd backend && uv run python ../scripts/spikes/s3_projection/report.py
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from pycrdt import Map, Text

from lae.corpus import SUBSETS, iter_corpus

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from s3_projection import projection  # noqa: E402
from s3_projection.projection import (  # noqa: E402
    JSON_KEY,
    ROOT_MAP,
    VALUE_KEY,
    canonical_json,
    hydrate,
    is_container,
    is_internal_node_iri,
    is_node,
    is_reference,
    project,
    strip_internal_iris,
)


def count_doc(value: Any, uri: str, stats: Counter[str]) -> None:
    """Count the shared types and special encodings in a hydrated document."""
    if isinstance(value, Text):
        stats["Text values"] += 1
    elif isinstance(value, Map):
        if is_container(value):
            stats["array containers"] += 1
            stats["array entries"] += len(value)
            stats["entries with a suffixed (duplicate) key"] += sum(
                1 for k, e in value.items() if e.get("id", k) != k and VALUE_KEY not in e
            )
        else:
            stats["node/reference maps"] += 1
            if is_internal_node_iri(value.get("id"), uri):
                stats["internal node IRIs minted"] += 1
        for child in value.values():
            count_doc(child, uri, stats)
    elif isinstance(value, dict) and JSON_KEY in value:
        stats["_json-encoded values"] += 1


def count_source(value: Any, stats: Counter[str], *, is_root: bool = True) -> None:
    """Count the source shapes the rule list has to decide."""
    if isinstance(value, dict):
        if not is_root and is_node(value) and not is_reference(value):
            stats["embedded nodes"] += 1
            if "id" in value:
                stats["embedded nodes with their own id"] += 1
            if "_label" in value:
                stats["embedded nodes with _label"] += 1
        for key, child in value.items():
            if isinstance(child, bool):
                stats["booleans"] += 1
            elif isinstance(child, int):
                stats["integers"] += 1
            elif isinstance(child, float):
                stats["floats"] += 1
                stats["floats with an integral value"] += child.is_integer()
            if isinstance(child, list):
                kinds = {
                    "ref" if is_reference(i) else "node" if is_node(i) else "opaque" for i in child
                }
                stats["arrays"] += 1
                stats["arrays: " + ("+".join(sorted(kinds)) or "empty")] += 1
                ids = [i["id"] for i in child if isinstance(i, dict) and "id" in i]
                stats["arrays repeating an id"] += len(set(ids)) < len(ids)
            if key == "@context":
                stats[f"@context as {type(child).__name__}"] += 1
            else:
                count_source(child, stats, is_root=False)
    elif isinstance(value, list):
        for child in value:
            count_source(child, stats, is_root=False)


def roundtrips(record: dict[str, Any]) -> bool:
    """The oracle for one record; any exception counts as a failure."""
    try:
        out = strip_internal_iris(project(hydrate(record)))
    except Exception:
        return False
    return canonical_json(out) == canonical_json(record)


def ablation(records: list[dict[str, Any]], **patches: Any) -> int:
    """Count the records that still round-trip with some rule functions replaced."""
    saved = {name: getattr(projection, name) for name in patches}
    try:
        for name, replacement in patches.items():
            setattr(projection, name, replacement)
        return sum(roundtrips(r) for r in records)
    finally:
        for name, original in saved.items():
            setattr(projection, name, original)


def main() -> None:
    for subset in SUBSETS:
        stats: Counter[str] = Counter()
        source: Counter[str] = Counter()
        records = [r for p, r in iter_corpus() if p.parent.name == subset]
        hydrate_s = project_s = 0.0
        size = update_size = 0
        for record in records:
            t0 = time.perf_counter()
            doc = hydrate(record)
            t1 = time.perf_counter()
            project(doc)
            t2 = time.perf_counter()
            hydrate_s += t1 - t0
            project_s += t2 - t1
            size += len(canonical_json(record))
            update_size += len(doc.get_update())
            count_doc(doc.get(ROOT_MAP, type=Map), record["id"], stats)
            count_source(record, source)
        files = len(records)
        print(f"## {subset}: {sum(roundtrips(r) for r in records)}/{files} round-trip")
        # Ablations: the guide's draft rules, taken literally.
        no_r9 = ablation(records, encode_plain=lambda v: v, decode_plain=lambda v: v)
        no_r7 = ablation(records, unique_key=lambda base, used: base)
        print(f"  without R9 (numbers and JSON stored as plain values): {no_r9}/{files}")
        print(f"  without the R7 suffix (entries keyed by bare id): {no_r7}/{files}")
        print(
            f"  hydrate {hydrate_s / files * 1000:.2f} ms/file, "
            f"project {project_s / files * 1000:.2f} ms/file"
        )
        print(
            f"  canonical JSON {size} bytes, Y update {update_size} bytes "
            f"({update_size / size:.2f}x)"
        )
        for name, n in sorted(source.items()):
            print(f"  source  {name}: {n}")
        for name, n in sorted(stats.items()):
            print(f"  doc     {name}: {n}")


if __name__ == "__main__":
    main()
