#!/usr/bin/env bash
# Spike S1, step 7: build a second ("green") index from scratch in another directory while the
# serving ("blue") server keeps answering queries, then start the green server on its own port.
#
#   scripts/spikes/s1_qlever/rebuild.sh <blue-port> <green-port> <green-dir> <input.nt> <out-dir>
#
# Writes <out-dir>/rebuild-{baseline,during}.json (blue query latency without and during the
# build), <out-dir>/rebuild-index.txt (wall-clock and peak RSS of the build) and prints the green
# server's start time. Stops only the green server. LAE_QLEVER_ACCESS_TOKEN must be set.
set -euo pipefail

blue_port=$1
green_port=$2
green_dir=$3
input=$4
out=$5
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
bench() { uv run --project "$repo/backend" python "$here/bench.py" "$@" 2>/dev/null; }
now() { python3 -c 'import time; print(time.time())'; }

mkdir -p "$out" "$green_dir"
rm -f "$green_dir"/lae.* "$out/rebuild.done"
ln -sf "$input" "$green_dir/input.nt"

# Blue latency with no build running, then during the build.
bench --port "$blue_port" --out "$out/rebuild-baseline.json" probe --seconds 20 --interval 0.05 >/dev/null
bench --port "$blue_port" --out "$out/rebuild-during.json" probe --until "$out/rebuild.done" --interval 0.05 >/dev/null &
probe=$!
(cd "$green_dir" && QLEVER_INPUT_FILES=input.nt "$here/peak_rss.sh" qlever-index "$out/rebuild-index.txt" -- \
  qlever --qleverfile "$repo/deploy/Qleverfile" index >"$out/rebuild-index.out" 2>&1)
touch "$out/rebuild.done"
wait "$probe"

t0=$(now)
make -C "$repo" up LAE_DB=lae_i6 QLEVER_PORT="$green_port" QLEVER_DIR="$green_dir" >"$out/rebuild-green-up.log" 2>&1
t1=$(now)
count=$(curl -sf -H "Accept: text/tab-separated-values" \
  --data-urlencode 'query=SELECT (COUNT(*) AS ?n) WHERE { ?s ?p ?o }' "http://localhost:$green_port" | tail -1)
make -C "$repo" down QLEVER_PORT="$green_port" QLEVER_DIR="$green_dir" >/dev/null 2>&1
python3 -c "import json; print(json.dumps({'green_start_s': round($t1 - $t0, 2), 'green_triples': '$count'}))"
