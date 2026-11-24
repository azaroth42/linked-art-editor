#!/usr/bin/env bash
# Phase 1 scale gate (Step 1.22): bootstrap 1M records through the production commands and
# record the timings in docs/spikes/S6-bootstrap.md.
#
#   1. Input: a deterministic 1M-record sample of the local LUX dump (scripts/load/sample_lux.py)
#      when the dump is present and complete; otherwise the S6 generator
#      (scripts/spikes/s6_bootstrap/generate.py, seed 20261122). The source used is recorded.
#   2. lae bootstrap aat-labels (when the AAT CSV is there), then the three bootstrap commands:
#      lae bootstrap load, lae bootstrap export-nt, lae bootstrap build-index. build-index leaves
#      the QLever server running; stop it with the command it prints (never a bare `qlever stop`).
#   3. Every stage's JSON summary goes to $WORK/summaries/, the wall-clock times to
#      $WORK/bootstrap-1m.json, and a section with both is appended to docs/spikes/S6-bootstrap.md
#      (RECORD_DOC=0 to skip that).
#
# Local only: never run this in CI (it needs a 1M load and QLever). Never fetch LUX online.
#
# Settings (environment):
#   LAE_DATABASE_URL         migrated database to load into (required); use an empty one
#   LAE_BASE_URI             record URI base of the installation (records are re-minted under it)
#   WORK                     scratch directory (default /tmp/lae-bootstrap-1m); needs ~40 GB
#   COUNT                    records to load (default 1000000)
#   LUX_DUMP                 directory of *.jsonl.gz (default /Users/rs2668/Development/lux/dataset/jsonl)
#   SOURCE                   auto (default), lux or generator
#   AAT_CSV                  linkedart-parser aat_dataset.csv (default ../linkedart-parser/data/aat_dataset.csv)
#   INDEX_NAME, QLEVER_PORT  QLever index name and port (default lae, 7010)
#   LAE_QLEVER_ACCESS_TOKEN  token for SPARQL Update on the new server (the sync worker needs it)
#   WORKERS, STREAMS         worker processes and COPY streams (lae defaults when unset)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${WORK:-/tmp/lae-bootstrap-1m}"
COUNT="${COUNT:-1000000}"
LUX_DUMP="${LUX_DUMP:-/Users/rs2668/Development/lux/dataset/jsonl}"
SOURCE="${SOURCE:-auto}"
AAT_CSV="${AAT_CSV:-$REPO/../linkedart-parser/data/aat_dataset.csv}"
INDEX_NAME="${INDEX_NAME:-lae}"
QLEVER_PORT="${QLEVER_PORT:-7010}"
RECORD_DOC="${RECORD_DOC:-1}"
: "${LAE_DATABASE_URL:?set LAE_DATABASE_URL to the migrated database to load}"

LAE=(uv run --project "$REPO/backend" --locked)
SUMMARIES="$WORK/summaries"
mkdir -p "$SUMMARIES"
now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }

# Run one stage, keep its JSON summary and its wall-clock time ($SUMMARIES/<name>.{json,time}).
stage() {
  local name="$1"
  shift
  echo "== $name: $*" >&2
  local start end
  start="$(now)"
  "$@" >"$SUMMARIES/$name.json" || return
  end="$(now)"
  python3 -c "print(round($end - $start, 1))" >"$SUMMARIES/$name.time"
  echo "   $name: $(cat "$SUMMARIES/$name.time") s" >&2
}

# 1. Input.
INPUT=""
if [[ "$SOURCE" != generator ]]; then
  if [[ -d "$LUX_DUMP" ]] && compgen -G "$LUX_DUMP/*.jsonl.gz" >/dev/null; then
    INPUT="$WORK/lux-${COUNT}.jsonl"
    if stage sample "${LAE[@]}" python "$REPO/scripts/load/sample_lux.py" \
      --dump "$LUX_DUMP" --count "$COUNT" --out "$INPUT"; then
      USED=lux
    else
      echo "   the LUX dump is incomplete or too small; using the S6 generator" >&2
      INPUT=""
    fi
  else
    echo "   no LUX dump in $LUX_DUMP; using the S6 generator" >&2
  fi
  if [[ -z "$INPUT" && "$SOURCE" == lux ]]; then
    echo "SOURCE=lux but the dump cannot be used" >&2
    exit 1
  fi
fi
if [[ -z "$INPUT" ]]; then
  INPUT="$WORK/generated-${COUNT}.jsonl"
  start="$(now)"
  "${LAE[@]}" python "$REPO/scripts/spikes/s6_bootstrap/generate.py" \
    --count "$COUNT" --out "$INPUT" >"$SUMMARIES/sample.json"
  end="$(now)"
  python3 -c "print(round($end - $start, 1))" >"$SUMMARIES/sample.time"
  USED=generator
fi
echo "== source: $USED ($INPUT)" >&2

# 2. The bootstrap. Optional tuning flags only when set, so the commands' defaults apply.
TUNING=()
[[ -n "${WORKERS:-}" ]] && TUNING+=(--workers "$WORKERS")
LOAD_TUNING=("${TUNING[@]+"${TUNING[@]}"}")
[[ -n "${STREAMS:-}" ]] && LOAD_TUNING+=(--streams "$STREAMS")

if [[ -f "$AAT_CSV" ]]; then
  stage aat-labels "${LAE[@]}" lae bootstrap aat-labels --csv "$AAT_CSV" \
    --report "$WORK/aat-labels-report.jsonl"
else
  echo "   no AAT CSV at $AAT_CSV; skipping aat-labels" >&2
fi
stage load "${LAE[@]}" lae bootstrap load --input "$INPUT" --report "$WORK/load-report.jsonl" \
  "${LOAD_TUNING[@]+"${LOAD_TUNING[@]}"}"
stage export-nt "${LAE[@]}" lae bootstrap export-nt --out "$WORK/records.nt" \
  "${TUNING[@]+"${TUNING[@]}"}"
rm -rf "$WORK/qlever"
stage build-index "${LAE[@]}" lae bootstrap build-index --nt "$WORK/records.nt" \
  --dir "$WORK/qlever" --name "$INDEX_NAME" --port "$QLEVER_PORT"

# 3. Record.
"${LAE[@]}" python - "$SUMMARIES" "$USED" "$COUNT" "$WORK/bootstrap-1m.json" \
  "$REPO/docs/spikes/S6-bootstrap.md" "$RECORD_DOC" <<'PY'
import json
import platform
import subprocess
import sys
from datetime import date
from pathlib import Path

summaries, used, count, out, doc, record_doc = sys.argv[1:7]
folder = Path(summaries)
stages = ["sample", "aat-labels", "load", "export-nt", "build-index"]
result = {"source": used, "count": int(count), "date": date.today().isoformat(), "stages": {}}
for name in stages:
    summary = folder / f"{name}.json"
    if not summary.exists():
        continue
    text = summary.read_text().strip()
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {"output": text[-2000:]}
    result["stages"][name] = {
        "wall_s": float((folder / f"{name}.time").read_text()),
        "summary": data,
    }
Path(out).write_text(json.dumps(result, indent=2) + "\n")

def get(name, *keys):
    value = result["stages"].get(name, {}).get("summary", {})
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value

walls = {name: stage["wall_s"] for name, stage in result["stages"].items()}
bootstrap = sum(walls.get(name, 0.0) for name in ("aat-labels", "load", "export-nt", "build-index"))
source = (
    f"a deterministic sample of the local LUX dump ({get('sample', 'dump_records'):,} records in "
    f"{get('sample', 'files')} files; bottom-{int(count):,} by keyed BLAKE2b of the id)"
    if used == "lux"
    else "the S6 generator (scripts/spikes/s6_bootstrap/generate.py, seed 20261122)"
)
rows = [
    ("input (" + used + ")", walls.get("sample"), ""),
    ("`aat-labels`", walls.get("aat-labels"), f"{get('aat-labels', 'terms') or 0:,} AAT terms"),
    (
        "`load`",
        walls.get("load"),
        f"{get('load', 'items'):,} read, {get('load', 'records_written'):,} written, "
        f"{get('load', 'records_skipped'):,} rejected, {get('load', 'records_flagged'):,} flagged",
    ),
    (
        "`export-nt`",
        walls.get("export-nt"),
        f"{get('export-nt', 'records'):,} records, {get('export-nt', 'triples'):,} triples",
    ),
    (
        "`build-index`",
        walls.get("build-index"),
        f"{get('build-index', 'triples_distinct') or 0:,} distinct triples, "
        f"peak RSS {get('build-index', 'peak_rss_gib')} GiB, "
        f"index {(get('build-index', 'index_bytes') or 0) / 1e9:.2f} GB",
    ),
]
lines = [
    "",
    f"## Phase 1 gate run (Step 1.22, {result['date']})",
    "",
    f"`scripts/load/bootstrap_1m.sh` on {platform.machine()} / macOS {platform.mac_ver()[0]}. "
    f"Source: **{used}**, {source}.",
    "",
    "| Stage | Wall | Notes |",
    "| :-- | --: | :-- |",
]
for label, wall, notes in rows:
    if wall is not None:
        lines.append(f"| {label} | {wall:.1f} s | {notes} |")
lines.append(f"| **Bootstrap (aat-labels + load + export-nt + build-index)** | **{bootstrap:.1f} s** | |")
lines.append("")
lines.append(f"Raw summaries: `{out}` (not committed).")
section = "\n".join(lines) + "\n"
print(section)
if record_doc == "1":
    with open(doc, "a", encoding="utf-8") as handle:
        handle.write(section)
PY
