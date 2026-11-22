#!/usr/bin/env bash
# Spike S1 (Phase 0 Step 0.6): the whole measurement sequence against one QLever binary.
#
#   LAE_QLEVER_ACCESS_TOKEN=<token> scripts/spikes/s1_qlever/run_s1.sh <label> [<bin-dir>]
#
# <label> names the results directory ($WORK/results/<label>/). With <bin-dir>, the qlever-index
# and qlever-server in that directory (e.g. a source build) are put first on PATH; otherwise the
# installed (Homebrew) binaries are used. Everything goes through deploy/Qleverfile and
# `make up` / `make down`. Ports and paths follow docs/agent-workflow.md slot 6 and can be
# overridden: PORT (7006), GREEN_PORT (7306), STUB_PORT (9806), WORK (/tmp/lae-i6).
#
# Steps: generate 10M triples (once), build the index (time, peak RSS), start the server, run
# the updates, text-freshness, restart/persistence, kill -9, SERVICE, and 1M-triple bulk
# measurements, build a second index while the first serves queries, stop everything.
set -euo pipefail

label=$1
bin_dir=${2:-}
PORT=${PORT:-7006}
GREEN_PORT=${GREEN_PORT:-7306}
STUB_PORT=${STUB_PORT:-9806}
WORK=${WORK:-/tmp/lae-i6}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
dir=$WORK/qlever-$label
out=$WORK/results/$label
: "${LAE_QLEVER_ACCESS_TOKEN:?set LAE_QLEVER_ACCESS_TOKEN}"
export LAE_QLEVER_ACCESS_TOKEN
if [ -n "$bin_dir" ]; then
  export PATH="$bin_dir:$PATH"
fi
bench() { uv run --project "$repo/backend" python "$here/bench.py" --port "$PORT" "$@" 2>/dev/null; }
log() { echo "== $label: $*" >&2; }

mkdir -p "$dir" "$out" "$WORK/data"
{
  echo "qlever-index: $(command -v qlever-index) ($(qlever-index --version 2>&1 | head -1))"
  echo "qlever-server: $(command -v qlever-server) ($(qlever-server --version 2>&1 | head -1))"
  echo "date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "machine: $(sysctl -n machdep.cpu.brand_string), $(sysctl -n hw.ncpu) cores, $(($(sysctl -n hw.memsize) / 1073741824)) GiB"
} | tee "$out/binary.txt" >&2

# 1. Data (deterministic; generated once and shared by every run).
if [ ! -s "$WORK/data/s1.nt" ]; then
  log "generating 10M triples"
  uv run --project "$repo/backend" python "$here/gen.py" --out "$WORK/data/s1.nt" 2>/dev/null
fi
ln -sf "$WORK/data/s1.nt" "$dir/s1.nt"

# 2. Index build through qlever-control and deploy/Qleverfile.
log "building the index"
make -C "$repo" down QLEVER_PORT="$PORT" QLEVER_DIR="$dir" >/dev/null 2>&1 || true
rm -f "$dir"/lae.*
(cd "$dir" && QLEVER_INPUT_FILES=s1.nt "$here/peak_rss.sh" qlever-index "$out/index.txt" -- \
  qlever --qleverfile "$repo/deploy/Qleverfile" index >"$out/index.out" 2>&1)
cp "$dir/lae.index-log.txt" "$out/index-log.txt"

# 3. Server with --persist-updates (PERSIST_UPDATES = true in the Qleverfile).
log "starting the server on port $PORT"
make -C "$repo" up LAE_DB=lae_i6 QLEVER_PORT="$PORT" QLEVER_DIR="$dir" >"$out/up.log" 2>&1
log "updates"
bench --out "$out/updates.json" updates >/dev/null

# 4. Text freshness.
log "text freshness"
bench --out "$out/text.json" text >/dev/null

# 5. Restart and persistence; then the kill -9 variant.
log "restart"
"$here/restart.sh" "$PORT" "$dir" >"$out/restart.json"
bench --out "$out/persist.json" persist >/dev/null
log "kill -9"
"$here/crash.sh" "$PORT" "$dir" >"$out/crash.json"

# 6. SERVICE with a VALUES sibling, against the stub.
log "SERVICE"
uv run --project "$repo/backend" python "$here/stub_service.py" --port "$STUB_PORT" \
  >"$out/stub.log" 2>&1 &
stub=$!
until curl -sf -o /dev/null "http://127.0.0.1:$STUB_PORT/log"; do sleep 0.1; done
bench --out "$out/service.json" service --stub "http://localhost:$STUB_PORT/sparql" >/dev/null || true
kill "$stub" 2>/dev/null || true
pkill -f "stub_service.py --port $STUB_PORT" 2>/dev/null || true

# Large delta: 1M inserted triples (replay rate, query latency), then a restart with it.
log "bulk 1M"
bench --out "$out/bulk.json" bulk >/dev/null
"$here/restart.sh" "$PORT" "$dir" >"$out/restart-1m-delta.json"

# 7. Rebuild from scratch into a second directory while this server keeps serving.
log "rebuild while serving"
"$here/rebuild.sh" "$PORT" "$GREEN_PORT" "$WORK/green-$label" "$WORK/data/s1.nt" "$out" \
  >"$out/rebuild-green.json"

# For comparison: QLever's own in-process rebuild (index + 1M delta triples -> new index).
log "in-process rebuild (cmd=rebuild-index)"
bench --out "$out/inplace-rebuild.json" inplace-rebuild --marker "$out/inplace.done" >/dev/null || true

log "stopping the server"
make -C "$repo" down QLEVER_PORT="$PORT" QLEVER_DIR="$dir" >/dev/null 2>&1
cp "$dir/lae.server-log.txt" "$out/server-log.txt" 2>/dev/null || true
log "done: $out"
