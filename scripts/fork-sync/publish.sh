#!/usr/bin/env bash
# Publishes the sync branch the fork-sync agent prepared: guards, push, draft pull request.
#
# Why this is a script and not the agent's job: pushing a branch that changes `.github/workflows/` needs a
# token with the `workflows` permission (FORK_SYNC_PUSH_TOKEN). The agent reads untrusted upstream content,
# so it never receives that token. This script runs in its own workflow step, from the DEFAULT branch's copy
# (the agent cannot edit it), and refuses to push unless every guard below holds.
#
# Guards:
#   1. the branch is named sync/upstream-YYYY-MM-DD and descends from the fork's default branch;
#   2. every workflow file the branch changes is byte-for-byte the file upstream has (or is deleted upstream),
#      so the agent cannot author workflow code, and the sync agent's own files are not touched;
#   3. the powerful token is used only when workflow files change; otherwise the default token pushes;
#   4. the branch is pushed under its own name, never forced, and never to the default branch;
#   5. a final commit carries `[skip ci]`, so the push does not run upstream's push/pull_request workflows
#      with this repository's secrets before a human has read them.
#
# All checks run in a FRESH repository fetched from the network and from the working copy's branch only:
# the agent could have planted hooks, config or fake refs in the working copy's `.git`.
#
# Env (required): BRANCH DEFAULT_BRANCH REPO UPSTREAM_REPO UPSTREAM_BRANCH DEFAULT_TOKEN WORKSPACE
#       (optional): PUSH_TOKEN PR_BODY_FILE GITHUB_OUTPUT REMOTE_URL UPSTREAM_URL SKIP_PR
# Exit: 0 published, 3 a guard refused, 4 the token needed for workflow changes is missing, 5 push or PR failed.
set -euo pipefail

: "${BRANCH:?}" "${DEFAULT_BRANCH:?}" "${REPO:?}" "${UPSTREAM_REPO:?}" "${UPSTREAM_BRANCH:?}" "${DEFAULT_TOKEN:?}" "${WORKSPACE:?}"
REMOTE_URL="${REMOTE_URL:-https://github.com/$REPO.git}"
UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/$UPSTREAM_REPO.git}"
[ -n "${PUSH_TOKEN:-}" ] && echo "::add-mask::$PUSH_TOKEN"
refuse() { echo "::error::fork-sync publish refused: $1" >&2; exit "${2:-3}"; }

[[ "$BRANCH" =~ ^sync/upstream-[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || refuse "branch name '$BRANCH' is not sync/upstream-YYYY-MM-DD"
[[ "$DEFAULT_BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || refuse "unexpected default branch name"

# A fresh repository: no hooks, no local config, no refs from the agent's working copy except the branch itself.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0
clean="$(mktemp -d)"
git init -q "$clean"
cd "$clean"
git config core.hooksPath /dev/null
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

# The default branch and upstream come from the network, not from the working copy.
auth() { printf 'AUTHORIZATION: basic %s' "$(printf 'x-access-token:%s' "$1" | base64 | tr -d '\n')"; }
git -c "http.extraheader=$(auth "$DEFAULT_TOKEN")" fetch -q --no-tags "$REMOTE_URL" "+refs/heads/$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH"
git fetch -q --no-tags "$UPSTREAM_URL" "+refs/heads/$UPSTREAM_BRANCH:refs/remotes/upstream/$UPSTREAM_BRANCH"
git fetch -q --no-tags "$WORKSPACE" "+refs/heads/$BRANCH:refs/heads/$BRANCH" || refuse "the branch $BRANCH does not exist in the working copy"

base="refs/remotes/origin/$DEFAULT_BRANCH"
up="refs/remotes/upstream/$UPSTREAM_BRANCH"
git merge-base --is-ancestor "$base" "$BRANCH" || refuse "the branch does not descend from $DEFAULT_BRANCH"

# The sync agent's own files must not change (the workflow's earlier step checks too: defence in depth).
own="$(git diff --name-only "$base...$BRANCH" -- .claude/skills/fork-sync .github/workflows/fork-sync.yml scripts/fork-sync script/fork-sync .backport-agent || true)"
[ -z "$own" ] || refuse "the branch changes the sync agent itself: $(echo "$own" | tr '\n' ' ')"

# Workflow files: only upstream's own version may enter.
workflows=()
while IFS= read -r file; do
  [ -n "$file" ] && workflows+=("$file")
done < <(git diff --name-only "$base...$BRANCH" -- .github/workflows)
for file in ${workflows[@]+"${workflows[@]}"}; do
  here="$(git rev-parse -q --verify "$BRANCH:$file" || echo gone)"
  there="$(git rev-parse -q --verify "$up:$file" || echo gone)"
  [ "$here" = "$there" ] || refuse "$file differs from upstream's version: workflow code must come from upstream verbatim"
done

token="$DEFAULT_TOKEN"
if [ "${#workflows[@]}" -gt 0 ]; then
  [ -n "${PUSH_TOKEN:-}" ] || refuse "the branch changes ${#workflows[@]} workflow file(s) and FORK_SYNC_PUSH_TOKEN is not set" 4
  token="$PUSH_TOKEN"
  echo "Workflow files from upstream (pushed with the workflows token): ${workflows[*]}"
fi

git checkout -q "$BRANCH"
git commit -q --allow-empty -m "ci(fork-sync): do not run push workflows on the sync branch [skip ci]"
git -c "http.extraheader=$(auth "$token")" push -q "$REMOTE_URL" "$BRANCH:refs/heads/$BRANCH" || refuse "git push failed" 5

[ -z "${SKIP_PR:-}" ] || exit 0
date_part="${BRANCH#sync/upstream-}"
title="Sync $UPSTREAM_BRANCH ($date_part)"
body_file="${PR_BODY_FILE:-}"
[ -n "$body_file" ] && [ -s "$body_file" ] || { body_file="$(mktemp)"; echo "Merges $UPSTREAM_REPO@$UPSTREAM_BRANCH. The agent left no description: read the diff and the run summary." > "$body_file"; }
# REST rather than `gh pr create`: some organisations' IP allow lists reject the GraphQL call.
url="$(GH_TOKEN="$DEFAULT_TOKEN" gh api "repos/$REPO/pulls" -f title="$title" -f head="$BRANCH" -f base="$DEFAULT_BRANCH" -F draft=true -F body=@"$body_file" --jq .html_url)" || refuse "the branch was pushed but the pull request could not be created" 5
echo "Draft pull request: $url"
[ -z "${GITHUB_OUTPUT:-}" ] || echo "pr=$url" >> "$GITHUB_OUTPUT"
