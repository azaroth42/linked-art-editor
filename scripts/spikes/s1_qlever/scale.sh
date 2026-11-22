#!/usr/bin/env bash
# Spike S1, step 7 (scaling points for the 1B extrapolation): build the index for one input file
# through deploy/Qleverfile and record wall-clock and peak RSS, then delete the index.
#
#   scripts/spikes/s1_qlever/scale.sh <input.nt> <log-file> [<bin-dir>]
#
# Generate inputs with gen.py --subjects N (100 triples per subject). With <bin-dir>, its
# qlever-index (e.g. a source build) is used instead of the installed one.
set -euo pipefail

input=$1
log=$2
bin_dir=${3:-}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
if [ -n "$bin_dir" ]; then
  export PATH="$bin_dir:$PATH"
fi
dir=$(mktemp -d "${WORK:-/tmp/lae-i6}/scale.XXXXXX")
ln -s "$input" "$dir/input.nt"
(cd "$dir" && QLEVER_INPUT_FILES=input.nt "$here/peak_rss.sh" qlever-index "$log" -- \
  qlever --qleverfile "$repo/deploy/Qleverfile" index >"$log.out" 2>&1)
cp "$dir/lae.index-log.txt" "$log.index-log.txt"
du -sk "$dir" | awk '{print "index_size_mib: " int($1 / 1024)}' >>"$log"
rm -rf "$dir"
cat "$log"
