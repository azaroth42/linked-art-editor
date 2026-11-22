#!/usr/bin/env bash
# Spike S1: run a command and report its wall-clock time and the peak RSS of every process whose
# command line matches a pattern (sampled every 0.5 s with ps), plus /usr/bin/time's maximum
# resident set size of the waited-for children.
#
#   scripts/spikes/s1_qlever/peak_rss.sh <ps-pattern> <log-file> -- <command> [args...]
#
# Example (index build through qlever-control, see run_s1.sh):
#   peak_rss.sh qlever-index /tmp/lae-i6/index-10m.txt -- qlever --qleverfile deploy/Qleverfile index
set -euo pipefail

pattern=$1
log=$2
shift 3

peak_file=$(mktemp)
echo 0 >"$peak_file"
(
  peak=0
  while :; do
    # RSS in KiB of all matching processes, summed (a pipeline may run several).
    rss=$(ps -axo rss=,command= | awk -v pat="$pattern" '$0 ~ pat && $0 !~ /awk|peak_rss/ {s += $1} END {print s + 0}')
    if [ "$rss" -gt "$peak" ]; then
      peak=$rss
      echo "$peak" >"$peak_file"
    fi
    sleep 0.5
  done
) &
sampler=$!

start=$(python3 -c 'import time; print(time.time())')
status=0
/usr/bin/time -l "$@" 2>"$log.time" || status=$?
end=$(python3 -c 'import time; print(time.time())')
kill "$sampler" 2>/dev/null || true
wait "$sampler" 2>/dev/null || true

wall=$(python3 -c "print(f'{$end - $start:.1f}')")
peak_kib=$(cat "$peak_file")
rm -f "$peak_file"
max_rss_bytes=$(awk '/maximum resident set size/ {print $1}' "$log.time")
{
  echo "command: $*"
  echo "exit_status: $status"
  echo "wall_clock_s: $wall"
  echo "peak_rss_sampled_mib: $((peak_kib / 1024))"
  echo "max_rss_children_mib: $((${max_rss_bytes:-0} / 1048576))"
  echo "load_average: $(sysctl -n vm.loadavg)"
} | tee "$log"
exit "$status"
