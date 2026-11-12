#!/usr/bin/env bash
# fork-sync (sctg-development/mistral-vibe)
#
# Deterministic pre-check for the weekly fork-sync agent. No LLM, no side effect on the repository.
# It decides whether the agent needs to run at all, so an up-to-date fork costs nothing.
#
# Prints key=value lines (and appends them to $GITHUB_OUTPUT when set):
#   behind          commits on upstream/main that this fork does not have
#   ahead           commits on this fork that upstream does not have
#   conflicts       number of files a plain merge would conflict on
#   conflict_files  comma-separated list of those files
#   open_sync_prs   open pull requests whose head branch is sync/upstream-*
#   needs_sync      true | false
#   reason          why needs_sync is false (up-to-date | open-sync-pr), empty otherwise
set -euo pipefail
cd "$(dirname "$0")/../.."

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"
BASE_BRANCH="${BASE_BRANCH:-multi}"
UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/mistralai/mistral-vibe.git}"

git remote get-url "$UPSTREAM_REMOTE" >/dev/null 2>&1 || git remote add "$UPSTREAM_REMOTE" "$UPSTREAM_URL"
git fetch -q "$UPSTREAM_REMOTE" "$UPSTREAM_BRANCH"
UP="$UPSTREAM_REMOTE/$UPSTREAM_BRANCH"

behind=$(git rev-list --count "HEAD..$UP")
ahead=$(git rev-list --count "$UP..HEAD")

conflicts=0
conflict_files=""
if [ "$behind" -gt 0 ]; then
  # merge-tree writes nothing to the working tree. Exit 1 means conflicts; the first line is a tree id,
  # then the conflicted file names up to the first blank line.
  set +e
  out=$(git merge-tree --write-tree --name-only HEAD "$UP" 2>/dev/null)
  rc=$?
  set -e
  if [ "$rc" -eq 1 ]; then
    conflict_files=$(printf '%s\n' "$out" | sed -n '2,$p' | sed '/^$/q' | sed '/^$/d' | paste -sd, -)
    conflicts=$(printf '%s' "$conflict_files" | tr ',' '\n' | grep -c . || true)
  fi
fi

open_sync_prs=0
if command -v gh >/dev/null 2>&1 && [ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]; then
  open_sync_prs=$(gh pr list --state open --base "$BASE_BRANCH" --json headRefName \
    --jq '[.[] | select(.headRefName | startswith("sync/upstream-"))] | length' 2>/dev/null || echo 0)
fi

needs_sync=true
reason=""
if [ "$behind" -eq 0 ]; then
  needs_sync=false; reason="up-to-date"
elif [ "$open_sync_prs" -gt 0 ]; then
  needs_sync=false; reason="open-sync-pr"
fi

emit() { echo "$1=$2"; [ -n "${GITHUB_OUTPUT:-}" ] && echo "$1=$2" >> "$GITHUB_OUTPUT" || true; }
emit behind "$behind"
emit ahead "$ahead"
emit conflicts "$conflicts"
emit conflict_files "$conflict_files"
emit open_sync_prs "$open_sync_prs"
emit needs_sync "$needs_sync"
emit reason "$reason"
