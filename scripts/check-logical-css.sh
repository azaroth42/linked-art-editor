#!/usr/bin/env bash
# Fail if frontend sources use physical-direction Tailwind utilities (conventions §5).
# Use the logical equivalents instead: ms-/me- (margin), ps-/pe- (padding), start-/end- (inset),
# text-start/text-end (alignment). Logical utilities mirror automatically in RTL layouts.
#
# Usage: scripts/check-logical-css.sh [directory ...]   (default: frontend/src)
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ $# -eq 0 ]]; then
  set -- "$root/frontend/src"
fi
for path in "$@"; do
  if [[ ! -e "$path" ]]; then
    echo "check-logical-css: no such file or directory: $path" >&2
    exit 2
  fi
done

# A utility starts a class token: at line start, after whitespace or a quote, after a variant
# (`md:`, `hover:`), or after `!` (important). An optional `-` allows negative values (`-ml-2`).
# This keeps words such as `html-`, `simple-`, or `copyright-` from matching.
boundary='(^|[[:space:]"'"'"'`:!])'
pattern="${boundary}-?(ml|mr|pl|pr|left|right)-|${boundary}text-(left|right)([^[:alnum:]_-]|$)"

status=0
matches="$(grep -rnE --include='*.ts' --include='*.tsx' --include='*.js' --include='*.jsx' \
  --include='*.css' --include='*.html' -e "$pattern" "$@")" || status=$?

case "$status" in
  0)
    echo "Physical-direction utilities found; use logical ones (ms-/me-, ps-/pe-, start-/end-," >&2
    echo "text-start/text-end) instead:" >&2
    echo "$matches" >&2
    exit 1
    ;;
  1)
    echo "check-logical-css: no physical-direction utilities in $*"
    ;;
  *)
    echo "check-logical-css: grep failed (exit $status)" >&2
    exit "$status"
    ;;
esac
