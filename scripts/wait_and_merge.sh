#!/usr/bin/env bash
# Wait for the CI run on a PR's head commit and squash-merge only if it succeeded.
# Usage: scripts/wait_and_merge.sh <pr-number>
# Used by the orchestrator (docs/agent-workflow.md). It never merges on "no checks reported".
set -u
n="$1"
sha=$(gh pr view "$n" --json headRefOid --jq .headRefOid)
id=""
for _ in $(seq 1 600); do
  id=$(gh run list --commit "$sha" --workflow CI --json databaseId --jq '.[0].databaseId // empty')
  [ -n "$id" ] && break
  sleep 2
done
[ -z "$id" ] && { echo "NO CI RUN for $sha"; exit 1; }
gh run watch "$id" --exit-status >/dev/null 2>&1
c=$(gh run view "$id" --json conclusion --jq .conclusion)
[ "$c" != "success" ] && { echo "CI $c on $sha; not merging"; exit 1; }
gh pr merge "$n" --squash --delete-branch && echo "merged #$n after CI success on ${sha:0:7}"
