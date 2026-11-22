#!/usr/bin/env bash
# Spike S1, step 5 (crash variant): send acknowledged updates, SIGKILL the server on <port> right
# after the last acknowledgement, restart it, and count how many of the updates survived.
#
#   scripts/spikes/s1_qlever/crash.sh <port> <index-dir>
#
# Only the qlever-server whose command line has `-p <port>` is killed.
set -euo pipefail

port=$1
dir=$2
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../../.." && pwd)
bench() { uv run --project "$repo/backend" python "$here/bench.py" --port "$port" "$@" 2>/dev/null; }

before=$(bench count-markers | python3 -c 'import json, sys; print(json.load(sys.stdin)["markers"])')
acked=$(bench marker -n 50 | python3 -c 'import json, sys; print(json.load(sys.stdin)["acked"])')
pid=$(pgrep -f "^\S*qlever-server .* -p $port ")
kill -9 "$pid"
while kill -0 "$pid" 2>/dev/null; do sleep 0.05; done
restart=$("$here/restart.sh" "$port" "$dir")
after=$(bench count-markers | python3 -c 'import json, sys; print(json.load(sys.stdin)["markers"])')
python3 - "$before" "$acked" "$after" "$restart" <<'EOF'
import json, sys
before, acked, after = map(int, sys.argv[1:4])
print(json.dumps({
    "markers_before": before,
    "acked_before_kill_9": acked,
    "markers_after_restart": after,
    "lost": before + acked - after,
    "restart": json.loads(sys.argv[4]),
}))
EOF
