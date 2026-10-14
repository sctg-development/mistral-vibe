# Fork customizations (sctg-development/mistral-vibe)

What this fork changes on top of upstream, so a sync keeps it. Upstream base:
`4ae5c595` (v2.26.1, merged in `9a9584e1`). Re-check with `git diff <upstream-tag> --stat -- vibe`.
Upstream remote: `upstream` = `https://github.com/mistralai/mistral-vibe.git`.

## Multi-account key pool (Vibe sign-in keys)

Goal: use many Vibe accounts (`mstrl_…` keys from `vibe --setup`, one per
account) from `vibe` and `vibe-acp`, with automatic failover and spreading.

### Files that are ours alone (no upstream counterpart, no merge conflict)
- `vibe/core/llm/key_pool.py` — pool (even spread: least-recent account with room, prompt cache sacrificed on purpose), cooldowns, rate-limit headroom
  (`x-ratelimit-*-minute` headers), in-flight spreading, per-key usage registry.
  State shared across processes in `$VIBE_HOME/key_pool_state.json`.
- `vibe/cli/list_keys.py` — `--list-keys`, `--export-keys`, `--export-keys-json`, `--import-keys`.
- `vibe/cli-rust/src/credentials/api_keys.rs` — Rust port of the key management flags (`vibe-rs` is what `vibe` runs
  when `VIBE_CLI=rust` or the rollout selects it). `--list-keys` queries console `/api/vibe/whoami` and API `/v1/users/me`
  per key (identity: 3 concurrent, 3 tries) and shares `$VIBE_HOME/whoami_cache_multi.json` (6 h TTL, key =
  first 32 hex of SHA-256) with the Python implementation. `--import-keys [KEYS]` takes the keys as an argument, or
  reads stdin when omitted or `-` (Python and Rust).
- `tests/backend/test_key_pool.py`, `tests/test_key_accounts.py`,
  `tests/cli/test_list_keys.py`.

### Upstream files we touched (re-apply after a sync; keep the hunks small)
| File | Change |
|---|---|
| `vibe/utils/api_keys.py` | `pool_env_var`, `pooled_keys`, `stored_pool_keys`, `add_pooled_key`; plural variable then keyring entry as fallback in `resolve_api_key_with_origin` |
| `vibe/utils/http.py` | `transport_wrapper` argument on `VibeAsyncHTTPClient` |
| `vibe/core/llm/backend/mistral.py` | builds the `KeyPool`, wraps the transport, records per-key usage (`_record_pool_usage`) |
| `vibe/setup/auth/api_key_persistence.py` | every sign-in is appended to the pool (`add_pooled_key`) |
| `vibe/_experimental_harness.py`, `vibe/app_server/_runtime.py` | `key_pool_active`: several keys force the legacy harness (only it can fail over; the native Unified harness bypasses our transport). Everything else follows upstream: Unified is the unconditional default, `--legacy-harness` the escape hatch. The old GrowthBook rollout-cache path (`cached_eval`) is gone: do not reintroduce it, upstream's cutover made it dead and it broke every e2e that needs the Unified harness |
| `vibe/app_server/_provider_credentials.py`, `_unified_harness_backend_adapter.py` | per-key usage attribution on the Unified path |
| `vibe/cli/session_exit.py` | per-key lines under the session total |
| `vibe/cli/entrypoint.py`, `vibe/cli/cli.py` | `--list-keys`, `--export-keys`, `--export-keys-json`, `--import-keys` flags |
| `vibe/cli-rust/src/cli.rs` | `--list-keys`, `--export-keys`, `--export-keys-json`, `--import-keys` CLI flags |
| `vibe/cli-rust/src/main.rs` | Key management flag handlers for Rust CLI |
| `vibe/cli-rust/src/session_exit.rs`, `src/server/process.rs`, `tests/session_exit.rs` | Rust TUI exit summary prints per-key usage under the session total. The app-server child runs in another process, so `key_pool.py` mirrors its usage (masked keys) to `$VIBE_KEY_USAGE_FILE`; the client sets that to `<tmp>/vibe-key-usage-<client pid>.json` at spawn and reads/removes it on exit |
| `vibe/cli-rust/src/credentials/mod.rs` | Added `api_keys` module to credentials; `default_key_present` probes `VIBE_API_KEY` (rename) |
| `vibe/cli-rust/src/setup/wizard/context.rs` | `DEFAULT_MISTRAL_API_ENV_KEY` = `VIBE_API_KEY` (rename); Rust tests that pin it (`setup_probe`, `onboarding_setup_scenarios`, `onboarding_submit_choices`) follow |
| `vibe/core/paths/*` | `whoami_cache_multi.json` path |

### Rename `MISTRAL_API_KEY(S)` → `VIBE_API_KEY(S)` (commit `01ae2bd2`)
Touches ~70 files, mostly tests and docs. The one line that matters is
`DEFAULT_MISTRAL_API_ENV_KEY = "VIBE_API_KEY"` in `vibe/core/config/_defaults.py`.
After a sync, re-run: `git grep -l MISTRAL_API_KEY` and rename only what refers
to the Vibe sign-in key, leaving third-party providers on their own variables.

### Tests we adapted (expect conflicts here after a sync)
Upstream tests still say `MISTRAL_API_KEY` and a single keyring read. Take upstream's side, then re-apply:
- the rename to `VIBE_API_KEY` on the lines upstream added (Python tests only; Rust tests keep `MISTRAL_API_KEY`);
- `tests/app_server/test_setup_store_credential.py`: a sign-in also writes `VIBE_API_KEYS` (pool), so the keyring write list has two entries;
- `tests/core/config/test_resolve_api_key.py`: the plural `…_KEYS` entry is a third, cached keyring read;
- `tests/app_server/test_provider_credentials.py`: keyring stubs take `**kwargs` (`search_legacy_services`).

## Sync procedure
1. `git fetch upstream && git merge upstream/main` (or rebase our commits).
2. Resolve conflicts in the table above; our own files will not conflict.
3. Re-run the rename check above.
4. `uv run pytest tests -n auto`, and compare any failure with the same run on the pre-merge commit (a `git worktree`) so only merge-induced failures are chased. If the `programmatic` e2e fail with "requires the Unified Harness", check `resolve_harness_selection` first.
5. Native module: a test run may rebuild `mistralai_vibe_local_harness/_native.abi3.so`
   (`uv build` via maturin); wait for it to finish before judging results.
