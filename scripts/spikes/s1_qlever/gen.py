"""Spike S1: generate synthetic N-Triples for the QLever update/text/rebuild measurements.

Writes ``--subjects`` subjects with exactly 100 triples each (the default, 100,000 subjects, gives
10,000,000 triples), deterministic for a given ``--seed``:

* 1 ``rdf:type`` and 1 ``rdfs:label`` (three pseudo-words plus the subject number, unique),
* 2 ``crm:P3_has_note`` descriptions of 8-12 pseudo-words (unique), 10 pooled material strings,
* 10 ``crm:P2_has_type`` links into a pool of 5,000 concepts, 40 links to other subjects,
* 20 ``xsd:decimal`` dimensions and 16 ``xsd:date`` values.

No pseudo-word starts with ``z``, so the only literal in the generated data that a search for
``zebra*`` can match is the control label of subject 0, ``"zebra-crossing-control"``. The
benchmark (``bench.py``) inserts ``"zebra-crossing-xyz"`` by SPARQL Update and compares the two.

    uv run --project backend python scripts/spikes/s1_qlever/gen.py --out /tmp/lae-i6/qlever/s1.nt
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

BASE = "http://localhost:8000/id/"
RDF_TYPE = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
RDFS_LABEL = "<http://www.w3.org/2000/01/rdf-schema#label>"
CRM = "http://www.cidoc-crm.org/cidoc-crm/"
EX = "https://example.org/s1/"
XSD = "http://www.w3.org/2001/XMLSchema#"
TRIPLES_PER_SUBJECT = 100
CONTROL_LABEL = "zebra-crossing-control"
CLASSES = [
    f"<{CRM}{name}>"
    for name in (
        "E22_Human-Made_Object",
        "E21_Person",
        "E74_Group",
        "E53_Place",
        "E55_Type",
        "E33_Linguistic_Object",
    )
]
LINK_PREDICATES = [
    f"<{CRM}{name}>"
    for name in (
        "P46_is_composed_of",
        "P67_refers_to",
        "P62_depicts",
        "P107_has_current_or_former_member",
        "P89_falls_within",
        "P130_shows_features_of",
        "P16_used_specific_object",
        "P14_carried_out_by",
    )
]
ONSETS = "b c d f g h j k l m n p r s t v w br cr dr fr gr pr tr bl cl fl gl pl sl st sp".split()
VOWELS = "a e i o u ai ea ou io".split()
CODAS = ["", "", "n", "r", "s", "l", "m", "nd", "rt", "st"]


def make_words(rng: random.Random, count: int) -> list[str]:
    """Distinct pseudo-words of 2-3 syllables; none starts with ``z``."""
    words: set[str] = set()
    while len(words) < count:
        syllables = rng.randint(2, 3)
        word = "".join(
            rng.choice(ONSETS) + rng.choice(VOWELS) + rng.choice(CODAS) for _ in range(syllables)
        )
        words.add(word)
    return sorted(words)


def literal(text: str) -> str:
    """An N-Triples plain literal (the generated text never needs escaping)."""
    return f'"{text}"'


def generate(out_path: Path, subjects: int, seed: int) -> int:
    """Write the triples to ``out_path`` and return how many were written."""
    rng = random.Random(seed)
    words = make_words(rng, 20_000)
    materials = [" ".join(rng.sample(words, 2)) for _ in range(2_000)]
    concepts = [f"<{BASE}concept/{k}>" for k in range(5_000)]
    dims = [f"<{EX}dim{k}>" for k in range(20)]
    dates = [f"<{EX}date{k}>" for k in range(16)]
    note = f"<{CRM}P3_has_note>"
    has_type = f"<{CRM}P2_has_type>"
    material = f"<{EX}material>"
    written = 0
    with out_path.open("w", encoding="utf-8", buffering=1 << 22) as out:
        for i in range(subjects):
            s = f"<{BASE}s1/{i}>"
            lines = [f"{s} {RDF_TYPE} {CLASSES[i % len(CLASSES)]} ."]
            if i == 0:
                label = CONTROL_LABEL
            else:
                label = " ".join(rng.sample(words, 3)) + f" {i}"
            lines.append(f"{s} {RDFS_LABEL} {literal(label)} .")
            for _ in range(2):
                text = " ".join(rng.choices(words, k=rng.randint(8, 12)))
                lines.append(f"{s} {note} {literal(text)} .")
            lines.extend(f"{s} {material} {literal(m)} ." for m in rng.sample(materials, 10))
            lines.extend(f"{s} {has_type} {c} ." for c in rng.sample(concepts, 10))
            for _ in range(40):
                target = f"<{BASE}s1/{rng.randrange(subjects)}>"
                lines.append(f"{s} {rng.choice(LINK_PREDICATES)} {target} .")
            for p in dims:
                value = f"{rng.randint(1, 5000)}.{rng.randint(0, 99):02d}"
                lines.append(f'{s} {p} "{value}"^^<{XSD}decimal> .')
            for p in dates:
                year, month, day = rng.randint(1400, 2025), rng.randint(1, 12), rng.randint(1, 28)
                lines.append(f'{s} {p} "{year}-{month:02d}-{day:02d}"^^<{XSD}date> .')
            assert len(lines) == TRIPLES_PER_SUBJECT
            out.write("\n".join(lines))
            out.write("\n")
            written += len(lines)
    return written


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description="Generate the Spike S1 synthetic N-Triples.")
    parser.add_argument("--out", type=Path, default=Path("s1.nt"), help="output N-Triples file")
    parser.add_argument("--subjects", type=int, default=100_000, help="100 triples per subject")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    started = time.monotonic()
    count = generate(args.out, args.subjects, args.seed)
    elapsed = time.monotonic() - started
    size = args.out.stat().st_size
    print(f"wrote {count:,} triples ({size / 1e9:.2f} GB) to {args.out} in {elapsed:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
