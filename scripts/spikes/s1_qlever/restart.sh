#!/usr/bin/env bash
# Spike S1, step 5: restart the QLever server on one port with `make down` / `make up` and time
# each part, then the first query that needs the persisted updates.
#
#   scripts/spikes/s1_qlever/restart.sh <port> <index-dir>
#
# Only the server on <port> is stopped (`make down` matches the port). LAE_QLEVER_ACCESS_TOKEN must
# be set. The qlever-server on PATH is used (run_s1.sh puts a source build first on PATH).
set -euo pipefail

port=$1
dir=$2
repo=$(cd "$(dirname "$0")/../../.." && pwd)
now() { python3 -c 'import time; print(time.time())'; }

t0=$(now)
make -C "$repo" down QLEVER_PORT="$port" QLEVER_DIR="$dir" >/dev/null 2>&1 || true
while pgrep -f "^\S*qlever-server .* -p $port " >/dev/null; do sleep 0.05; done
t1=$(now)
make -C "$repo" up LAE_DB="lae_i6" QLEVER_PORT="$port" QLEVER_DIR="$dir" >"$dir/../restart-up.log" 2>&1
t2=$(now)
query='SELECT (COUNT(*) AS ?n) WHERE { <http://localhost:8000/id/s1/text-fresh> ?p ?o }'
until curl -sf -o /dev/null -H "Authorization: Bearer $LAE_QLEVER_ACCESS_TOKEN" \
  --data-urlencode "query=$query" "http://localhost:$port"; do
  sleep 0.05
done
t3=$(now)
python3 - "$t0" "$t1" "$t2" "$t3" <<'EOF'
import json, sys
t0, t1, t2, t3 = map(float, sys.argv[1:])
print(json.dumps({
    "stop_s": round(t1 - t0, 2),
    "start_until_ready_s": round(t2 - t1, 2),
    "first_query_after_ready_s": round(t3 - t2, 2),
    "restart_total_s": round(t3 - t0, 2),
}))
EOF
