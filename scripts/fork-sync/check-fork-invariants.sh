#!/usr/bin/env bash
# Deterministic gate for the fork: checks that what FORK_CUSTOMIZATIONS.md promises is still in the tree after
# a sync with upstream. It reads the repository only and prints one line per check. Exit 1 when any check fails.
#
# This is the contract between the sync agent and the maintainer: the agent may resolve conflicts however it
# sees fit, but a sync is never reported as done unless this script ends with "0 failed". When a customization
# is deliberately changed, change the matching check here in the same commit.
set -uo pipefail
cd "$(dirname "$0")/../.."

pass=0
fail=0
ok() { pass=$((pass + 1)); echo "ok    $1"; }
ko() { fail=$((fail + 1)); echo "FAIL  $1"; }
has_file() { [ -f "$1" ] && ok "file $1" || ko "file $1 is missing"; }
has_text() { grep -qF -- "$2" "$1" 2>/dev/null && ok "$3" || ko "$3 (expected \"$2\" in $1)"; }
lacks_text() { grep -qF -- "$2" "$1" 2>/dev/null && ko "$3 (found \"$2\" in $1)" || ok "$3"; }

# --- Files that are ours alone -------------------------------------------------------------------------------
for f in \
  vibe/core/llm/key_pool.py \
  vibe/cli/list_keys.py \
  vibe/cli-rust/src/credentials/api_keys.rs \
  tests/backend/test_key_pool.py \
  tests/test_key_accounts.py \
  tests/app_server/test_provider_credentials_pool.py \
  FORK_CUSTOMIZATIONS.md; do
  has_file "$f"
done

# --- The pool: helpers, and the one place that uses them ---------------------------------------------------
for fn in pool_env_var pooled_keys stored_pool_keys add_pooled_key split_keys; do
  has_text vibe/utils/api_keys.py "def $fn(" "api_keys.$fn exists"
done
has_text vibe/utils/api_keys.py "uses_cli_oidc_credential" "OIDC switch still present (upstream)"
has_text vibe/utils/api_keys.py '"VIBE_API_KEY"' "OIDC switch accepts the renamed key"
has_text vibe/app_server/_provider_credentials.py "_pooled_snapshot" "pool is taken at every model call"
has_text vibe/app_server/_provider_credentials.py "self._leases.pop(observed_revision" "a rejection parks the leased account"
has_text vibe/app_server/_provider_credentials.py "uses_cli_oidc_credential(env_var)" "the pool never overrides a CLI OIDC session"
has_text vibe/setup/auth/api_key_persistence.py "add_pooled_key" "every sign-in joins the pool"

# The pool must be taken BEFORE the single-key resolution, or it is dead code. _current_snapshot is a method
# of ProviderCredentialService: its call order is what matters, so compare the line numbers.
pooled_line=$(grep -n "self._pooled_snapshot" vibe/app_server/_provider_credentials.py | head -1 | cut -d: -f1)
single_line=$(grep -n "_resolve_snapshot, provider, self._credentials" vibe/app_server/_provider_credentials.py | head -1 | cut -d: -f1)
if [ -n "$pooled_line" ] && [ -n "$single_line" ] && [ "$pooled_line" -lt "$single_line" ]; then
  ok "the pool is consulted before the single key"
else
  ko "the pool is not consulted before the single key (lines: pool=${pooled_line:-none} single=${single_line:-none})"
fi

# --- Runtime dependencies: httpx is a DEV dependency, the runtime client is httpx2 ----------------------------
# A stray `import httpx` passes every test (the dev environment has it) and crashes the packaged app at startup.
# scripts/fork-sync/check-runtime-imports.py is the exact check; this one is the cheap static version of it.
if grep -rEn '^[[:space:]]*(import httpx$|from httpx )' vibe --include='*.py' >/dev/null 2>&1; then
  ko "a vibe module imports httpx, a dev-only dependency (use httpx2)"
else
  ok "no vibe module imports the dev-only httpx"
fi
has_file scripts/fork-sync/check-runtime-imports.py

# --- Rename MISTRAL_API_KEY -> VIBE_API_KEY (only the Vibe sign-in key) --------------------------------------
has_text vibe/core/config/_defaults.py 'DEFAULT_MISTRAL_API_ENV_KEY = "VIBE_API_KEY"' "Python default env key is VIBE_API_KEY"
has_text vibe/cli-rust/src/setup/wizard/context.rs 'DEFAULT_MISTRAL_API_ENV_KEY: &str = "VIBE_API_KEY"' "Rust default env key is VIBE_API_KEY"
has_text vibe/app_server/_credential_selection.py 'MISTRAL_API_KEY_ENV = "VIBE_API_KEY"' "platform credential env key is VIBE_API_KEY"

# --- Rust key management and exit summary ----------------------------------------------------------------------
for flag in list-keys export-keys export-keys-json import-keys; do
  has_text vibe/cli-rust/src/cli.rs "long = \"$flag\"" "Rust CLI flag --$flag"
done
has_text vibe/cli-rust/src/server/process.rs "KEY_USAGE_FILE_ENV_VAR" "app-server child gets the key usage file"
has_text vibe/cli-rust/src/credentials/mod.rs "api_keys" "credentials module exposes api_keys"
has_text vibe/core/paths/_vibe_home.py "whoami_cache_multi.json" "whoami_cache_multi.json path"

# --- Release workflow ----------------------------------------------------------------------------------------------
has_text .github/workflows/release.yml "softprops/action-gh-release" "release publishes to GitHub Releases"
has_text .github/workflows/release.yml "attest-build-provenance" "release attests its artifacts"

# --- The sync agent must not be edited by a sync branch (checked by the workflow too) ---------------------------
has_file .claude/skills/fork-sync/SKILL.md
has_file .github/workflows/fork-sync.yml
has_file scripts/fork-sync/precheck.sh
lacks_text .github/workflows/fork-sync.yml "schedule:" "fork-sync has no schedule (the openclaw cron is the only trigger)"

echo "===================================="
echo "Invariant checks: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
