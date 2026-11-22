"""Synthetic Linked Art records for Spike S6, varied from the golden corpus templates.

Record ``i`` depends only on ``(seed, i)``: its class comes from one seeded stream over
``TYPE_MIX``, and everything else from ``random.Random(f"{seed}:{i}")``. Runs are therefore
reproducible byte for byte, whatever the number of worker processes.

Each record is built like this:

1. Pick a primary template of the record's class from ``tests/corpus/`` (institutional records
   weighted ``INSTITUTIONAL_WEIGHT`` times a linked.art example), then merge 0-4 donor templates of
   the same class into it (list properties are concatenated, missing properties copied), so records
   combine the patterns of several examples and have realistic sizes.
2. Give it a UUIDv7 record URI and, for every embedded node without an ``id``, an internal node IRI
   ``<record>/node/<uuidv7>`` (S3 rule R4; Phase 1 bootstrap assigns these before ``COPY``).
3. Point references to local entities at other synthetic records of the same class, keep
   authority references (AAT, ULAN, Wikidata, ...) as they are, and give web resources new paths.
4. Vary literals: names from corpus words and synthetic proper names, identifiers with new digits,
   statements resampled from corpus words (Zipf) with a log-normal length factor, dates shifted
   by one offset per record, dimension values scaled.
5. Inject one sanitation defect into a ``defect_rate`` fraction of records, so the export's
   sanitation counters can be checked against known numbers.

Output is JSON Lines, one canonical (sorted-key) record per line: the input format of
``lae bootstrap load`` (Phase 1, Step 1.12).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import time
import uuid
from array import array
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from itertools import accumulate
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import orjson

from lae.corpus import iter_corpus

TYPE_MIX: Final[dict[str, float]] = {
    "HumanMadeObject": 35.0,
    "Person": 15.0,
    "LinguisticObject": 15.0,
    "VisualItem": 8.0,
    "Type": 8.0,
    "Group": 5.0,
    "Place": 4.0,
    "Set": 4.0,
    "DigitalObject": 3.0,
    "Activity": 2.0,
    "Event": 0.5,
    "Period": 0.25,
    "PropositionalObject": 0.25,
}
"""Share (%) of each top-level class: a museum-plus-library catalogue (see the S6 report)."""

TYPES: Final = tuple(TYPE_MIX)
TYPE_CUM_WEIGHTS: Final = tuple(accumulate(TYPE_MIX.values()))

CONTEXT_URL: Final = "https://linked.art/ns/v1/linked-art.json"
DEFAULT_BASE_URI: Final = "https://collection.example.org/id/"
DEFAULT_SEED: Final = 20261122
DEFAULT_DEFECT_RATE: Final = 1e-4
BASE_MS: Final = 1_767_225_600_000
"""UUIDv7 timestamp of record 0 (2026-01-01T00:00:00Z); 16 records are minted per millisecond."""
RECORDS_PER_MS: Final = 16

INSTITUTIONAL_WEIGHT: Final = 4
DONOR_COUNTS: Final = (0, 0, 1, 1, 2, 3, 4)

AUTHORITY_HOSTS: Final = frozenset(
    {
        "vocab.getty.edu",
        "www.wikidata.org",
        "wikidata.org",
        "id.loc.gov",
        "viaf.org",
        "qudt.org",
        "creativecommons.org",
        "rightsstatements.org",
        "www.w3.org",
        "w3.org",
        "sws.geonames.org",
        "www.geonames.org",
    }
)
"""Hosts whose IRIs are shared vocabulary: references to them are kept unchanged."""

WEB_RESOURCE_KEYS: Final = frozenset({"access_point", "conforms_to", "equivalent", "format"})
REF_KEYS: Final = frozenset({"id", "type", "_label"})
TEXT_TYPES: Final = frozenset({"Name", "Identifier", "LinguisticObject"})
DATE_KEYS: Final = frozenset(
    {"begin_of_the_begin", "end_of_the_begin", "begin_of_the_end", "end_of_the_end"}
)
AGENT_TYPES: Final = frozenset({"Person", "Group"})
DEFECTS: Final = ("iri_space", "iri_unfixable", "date_partial", "date_garbage", "literal_special")
SYLLABLES: Final = (
    "an ar bel ber bri ca cor dal del dor el en es fa fen gar gil gra ham har ia in ka "
    "ker la ler li lo ly mar mel mon na nor o pa per ra ri ro sa sel son ta ter ti ton "
    "va ven vin wel win ya"
).split()

_WORD: Final = re.compile(r"[A-Za-z][A-Za-z'-]+")
_DIGIT: Final = re.compile(r"\d")
_YEAR: Final = re.compile(r"(-?\d{4})(.*)", re.S)


def record_uuid(seed: int, index: int) -> uuid.UUID:
    """The UUIDv7 of record ``index``: time-ordered, random bits derived from ``(seed, index)``."""
    ms = BASE_MS + index // RECORDS_PER_MS
    rand = int.from_bytes(hashlib.blake2b(f"{seed}:{index}".encode(), digest_size=10).digest())
    return _uuid7(ms, rand)


def _uuid7(ms: int, rand: int) -> uuid.UUID:
    rand_a = (rand >> 62) & 0xFFF
    rand_b = rand & ((1 << 62) - 1)
    return uuid.UUID(int=(ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b)


def type_codes(seed: int, count: int) -> bytes:
    """Class of every record as an index into ``TYPES`` (prefix-stable: record i ignores count)."""
    rng = random.Random(f"{seed}:types")
    return bytes(rng.choices(range(len(TYPES)), cum_weights=TYPE_CUM_WEIGHTS, k=count))


def _corpus_words() -> tuple[list[str], list[float]]:
    """Words of corpus literals by descending frequency, with Zipf cumulative weights."""
    counts: Counter[str] = Counter()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("content", "_label") and isinstance(item, str):
                    counts.update(_WORD.findall(item))
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for _, record in iter_corpus():
        walk(record)
    words = [word for word, _ in counts.most_common()]
    return words, list(accumulate(1.0 / (rank + 1) for rank in range(len(words))))


class Generator:
    """Builds synthetic record ``i`` for a fixed ``(seed, count)``; one instance per process."""

    def __init__(
        self,
        seed: int,
        count: int,
        codes: bytes | None = None,
        base_uri: str = DEFAULT_BASE_URI,
        defect_rate: float = DEFAULT_DEFECT_RATE,
    ) -> None:
        self.seed = seed
        self.count = count
        self.codes = codes if codes is not None else type_codes(seed, count)
        self.base_uri = base_uri
        self.defect_rate = defect_rate
        self.pools: dict[str, array[int]] = {name: array("I") for name in TYPES}
        for index, code in enumerate(self.codes):
            self.pools[TYPES[code]].append(index)
        self.templates: dict[str, list[bytes]] = {name: [] for name in TYPES}
        self.template_weights: dict[str, list[int]] = {name: [] for name in TYPES}
        for path, record in iter_corpus():
            kind = record.get("type")
            if kind in self.templates:
                self.templates[kind].append(orjson.dumps(record))
                weight = INSTITUTIONAL_WEIGHT if path.parent.name == "institutional" else 1
                self.template_weights[kind].append(weight)
        self.words, self.word_cum = _corpus_words()

    def uri(self, index: int) -> str:
        """Public URI of record ``index``."""
        return self.base_uri + str(record_uuid(self.seed, index))

    def record(self, index: int) -> tuple[dict[str, Any], str | None]:
        """Build record ``index``; return it and the injected defect kind, if any."""
        rng = random.Random(f"{self.seed}:{index}")
        kind = TYPES[self.codes[index]]
        templates, weights = self.templates[kind], self.template_weights[kind]
        record: dict[str, Any] = orjson.loads(rng.choices(templates, weights=weights)[0])
        if len(templates) > 2:
            for _ in range(rng.choice(DONOR_COUNTS)):
                _merge(record, orjson.loads(rng.choice(templates)))
        uri = self.uri(index)
        record["@context"] = CONTEXT_URL
        record["id"] = uri
        varier = _Varier(self, rng, uri, BASE_MS + index // RECORDS_PER_MS, kind)
        varier.walk(record, 0, "")
        varier.set_root_label(record)
        defect = None
        if rng.random() < self.defect_rate:
            defect = varier.inject_defect(record, rng.choice(DEFECTS))
        return record, defect


def _merge(record: dict[str, Any], donor: dict[str, Any]) -> None:
    for key, value in donor.items():
        if key in ("@context", "id", "type", "_label"):
            continue
        if key not in record:
            record[key] = value
        elif isinstance(record[key], list) and isinstance(value, list):
            record[key].extend(value)


class _Varier:
    """Per-record state for the variation walk (id remapping, date offset, value scale)."""

    def __init__(
        self, gen: Generator, rng: random.Random, uri: str, ms: int, record_type: str
    ) -> None:
        self.gen = gen
        self.rng = rng
        self.uri = uri
        self.ms = ms
        self.record_type = record_type
        self.remap: dict[str, str] = {}
        self.year_shift = rng.randint(-150, 60)
        self.scale = rng.uniform(0.5, 2.0)
        self.date_holders: list[tuple[dict[str, Any], str]] = []

    def node_iri(self) -> str:
        return f"{self.uri}/node/{_uuid7(self.ms, self.rng.getrandbits(74))}"

    def walk(self, node: dict[str, Any], depth: int, parent_key: str) -> None:
        kind = node.get("type")
        if depth > 0:
            ident = node.get("id")
            if isinstance(ident, str):
                node["id"] = self.remap_id(ident, kind, parent_key, set(node) <= REF_KEYS)
            elif isinstance(kind, str):
                node["id"] = self.node_iri()
        for key, value in node.items():
            if isinstance(value, dict):
                self.walk(value, depth + 1, key)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        self.walk(item, depth + 1, key)
            elif key == "content" and isinstance(value, str) and kind in TEXT_TYPES:
                node[key] = self.vary_content(kind, value)
            elif key in DATE_KEYS and isinstance(value, str):
                node[key] = self.shift_date(value)
                self.date_holders.append((node, key))
            elif key == "value" and type(value) in (int, float):
                scaled = value * self.scale
                node[key] = round(scaled, 2) if type(value) is float else max(1, round(scaled))

    def remap_id(self, ident: str, kind: Any, parent_key: str, is_reference: bool) -> str:
        new = self.remap.get(ident)
        if new is not None:
            return new
        host = urlsplit(ident).netloc
        if host in AUTHORITY_HOSTS:
            new = ident
        elif parent_key in WEB_RESOURCE_KEYS:
            new = self.new_path(ident)
        elif kind in self.gen.pools and self.gen.pools[kind]:
            pool = self.gen.pools[kind]
            new = self.gen.uri(pool[self.rng.randrange(len(pool))])
        elif not is_reference:
            new = self.node_iri()
        else:
            new = self.new_path(ident)
        self.remap[ident] = new
        return new

    def new_path(self, iri: str) -> str:
        """Same host, new digits in the path (or a numeric suffix if it has none)."""
        cut = iri.find("/", iri.find("//") + 2) if "//" in iri else -1
        if cut < 0:
            return f"{iri}/{self.rng.getrandbits(32)}"
        head, path = iri[:cut], iri[cut:]
        if not _DIGIT.search(path):
            return f"{iri}-{self.rng.getrandbits(32)}"
        return head + _DIGIT.sub(lambda _: str(self.rng.randrange(10)), path)

    def word(self) -> str:
        return self.rng.choices(self.gen.words, cum_weights=self.gen.word_cum)[0]

    def proper_name(self) -> str:
        parts = self.rng.choices(SYLLABLES, k=self.rng.randint(2, 3))
        return "".join(parts).capitalize()

    def vary_content(self, kind: str, content: str) -> str:
        rng = self.rng
        if kind == "Identifier":
            if _DIGIT.search(content):
                return _DIGIT.sub(lambda _: str(rng.randrange(10)), content)
            return f"{content} {rng.randrange(100_000)}"
        tokens = content.split()
        if kind == "Name":
            if not tokens:
                return content
            make = self.proper_name if self.record_type in AGENT_TYPES else self.word
            changed = [make() if rng.random() < 0.4 else token for token in tokens]
            if changed == tokens:
                changed[rng.randrange(len(changed))] = make()
            return " ".join(changed)
        length = max(3, int(len(tokens) * rng.lognormvariate(0.0, 0.5)))
        words = rng.choices(self.gen.words, cum_weights=self.gen.word_cum, k=length)
        return " ".join(words).capitalize() + "."

    def shift_date(self, value: str) -> str:
        match = _YEAR.fullmatch(value)
        if match is None:
            return value
        year = int(match[1]) + self.year_shift
        if not 1 <= year <= 2025:
            return value
        rest = match[2]
        if rest.startswith("-02-29"):
            rest = "-02-28" + rest[6:]
        return f"{year:04d}{rest}"

    def set_root_label(self, record: dict[str, Any]) -> None:
        for name in record.get("identified_by", ()):
            if isinstance(name, dict) and name.get("type") == "Name":
                content = name.get("content")
                if isinstance(content, str) and content:
                    record["_label"] = content
                    return
        label = record.get("_label")
        if isinstance(label, str) and label:
            record["_label"] = self.vary_content("Name", label)

    def inject_defect(self, record: dict[str, Any], kind: str) -> str:
        """Add one sanitation defect of ``kind``; return the kind actually injected."""
        if kind in ("date_partial", "date_garbage") and not self.date_holders:
            kind = "literal_special"
        if kind == "iri_space":
            bad = {"id": "http://vocab.getty.edu/aat/300404670 x", "type": "Type"}
            record.setdefault("classified_as", []).append(bad)
        elif kind == "iri_unfixable":
            bad = {"id": "https:/no-host/defect", "type": "Type"}
            record.setdefault("classified_as", []).append(bad)
        elif kind in ("date_partial", "date_garbage"):
            holder, key = self.rng.choice(self.date_holders)
            year = holder[key][:4]
            holder[key] = year if kind == "date_partial" else f"c. {year}"
        else:
            record["_label"] = f'{record.get("_label", "")} "quoted" back\\slash\nnew\tline'
        return kind


_GEN: Generator | None = None


def _init_worker(seed: int, count: int, codes: bytes, base_uri: str, defect_rate: float) -> None:
    global _GEN
    _GEN = Generator(seed, count, codes, base_uri, defect_rate)


def generate_chunk(start: int, stop: int) -> tuple[bytes, dict[str, Any]]:
    """Worker: canonical JSON Lines for records ``start..stop-1`` and their statistics."""
    assert _GEN is not None
    lines: list[bytes] = []
    types: Counter[str] = Counter()
    defects: Counter[str] = Counter()
    sizes = array("I")
    for index in range(start, stop):
        record, defect = _GEN.record(index)
        line = orjson.dumps(record, option=orjson.OPT_SORT_KEYS)
        lines.append(line)
        types[record["type"]] += 1
        sizes.append(len(line))
        if defect is not None:
            defects[defect] += 1
    lines.append(b"")
    return b"\n".join(lines), {"types": types, "defects": defects, "sizes": sizes}


def iter_records(
    count: int, seed: int = DEFAULT_SEED, defect_rate: float = DEFAULT_DEFECT_RATE
) -> Iterator[tuple[dict[str, Any], str | None]]:
    """In-process generator for tests and small samples."""
    gen = Generator(seed, count, defect_rate=defect_rate)
    for index in range(count):
        yield gen.record(index)


def _percentile(sorted_values: list[int], q: float) -> int:
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


def main(argv: list[str] | None = None) -> int:
    """Write ``--count`` synthetic records to ``--out`` and print a JSON summary."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--count", type=int, default=1_000_000)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--chunk", type=int, default=10_000)
    parser.add_argument("--base-uri", default=DEFAULT_BASE_URI)
    parser.add_argument("--defect-rate", type=float, default=DEFAULT_DEFECT_RATE)
    parser.add_argument("--stats-out", type=Path)
    args = parser.parse_args(argv)

    started = time.perf_counter()
    codes = type_codes(args.seed, args.count)
    starts = list(range(0, args.count, args.chunk))
    stops = [min(start + args.chunk, args.count) for start in starts]
    types: Counter[str] = Counter()
    defects: Counter[str] = Counter()
    sizes: list[int] = []
    digest = hashlib.sha256()
    total_bytes = 0
    init_args = (args.seed, args.count, codes, args.base_uri, args.defect_rate)
    with (
        ProcessPoolExecutor(args.workers, initializer=_init_worker, initargs=init_args) as pool,
        args.out.open("wb") as out,
    ):
        for data, stats in pool.map(generate_chunk, starts, stops):
            out.write(data)
            digest.update(data)
            total_bytes += len(data)
            types.update(stats["types"])
            defects.update(stats["defects"])
            sizes.extend(stats["sizes"])
    elapsed = time.perf_counter() - started
    sizes.sort()
    summary = {
        "stage": "generate",
        "count": args.count,
        "seed": args.seed,
        "workers": args.workers,
        "seconds": round(elapsed, 2),
        "records_per_s": round(args.count / elapsed),
        "bytes": total_bytes,
        "sha256": digest.hexdigest(),
        "types": dict(types.most_common()),
        "defects": dict(sorted(defects.items())),
        "record_bytes": {
            "mean": round(total_bytes / max(1, args.count)),
            "p50": _percentile(sizes, 0.5),
            "p90": _percentile(sizes, 0.9),
            "p99": _percentile(sizes, 0.99),
            "max": sizes[-1] if sizes else 0,
        },
    }
    text = json.dumps(summary, indent=2)
    print(text)
    if args.stats_out:
        args.stats_out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
