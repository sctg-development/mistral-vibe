# Fork customizations (sctg-development/mistral-vibe)

What this fork changes on top of upstream, so a sync keeps it. Upstream base:
`7c19608a` (v2.25.8). Re-check with `git diff <upstream-tag> --stat -- vibe`.

## Multi-account key pool (Vibe sign-in keys)

Goal: use many Vibe accounts (`mstrl_…` keys from `vibe --setup`, one per
account) from `vibe` and `vibe-acp`, with automatic failover and spreading.

### Files that are ours alone (no upstream counterpart, no merge conflict)
- `vibe/core/llm/key_pool.py` — pool (even spread: least-recent account with room, prompt cache sacrificed on purpose), cooldowns, rate-limit headroom
  (`x-ratelimit-*-minute` headers), in-flight spreading, per-key usage registry.
  State shared across processes in `$VIBE_HOME/key_pool_state.json`.
- `vibe/cli/list_keys.py` — `--list-keys`, `--export-keys`, `--export-keys-json`, `--import-keys`.
- `vibe/cli-rust/src/credentials/api_keys.rs` — Rust implementation of key management functions.
- `tests/backend/test_key_pool.py`, `tests/test_key_accounts.py`,
  `tests/cli/test_list_keys.py`.

### Upstream files we touched (re-apply after a sync; keep the hunks small)
| File | Change |
|---|---|
| `vibe/utils/api_keys.py` | `pool_env_var`, `pooled_keys`, `stored_pool_keys`, `add_pooled_key`; plural variable then keyring entry as fallback in `resolve_api_key_with_origin` |
| `vibe/utils/http.py` | `transport_wrapper` argument on `VibeAsyncHTTPClient` |
| `vibe/core/llm/backend/mistral.py` | builds the `KeyPool`, wraps the transport, records per-key usage (`_record_pool_usage`) |
| `vibe/setup/auth/api_key_persistence.py` | every sign-in is appended to the pool (`add_pooled_key`) |
| `vibe/_experimental_harness.py`, `vibe/app_server/_runtime.py` | `key_pool_active`: several keys force the legacy harness (only it can fail over; the native Unified harness bypasses our transport) |
| `vibe/app_server/_provider_credentials.py`, `_unified_harness_backend_adapter.py` | per-key usage attribution on the Unified path |
| `vibe/cli/session_exit.py` | per-key lines under the session total |
| `vibe/cli/entrypoint.py`, `vibe/cli/cli.py` | `--list-keys`, `--export-keys`, `--export-keys-json`, `--import-keys` flags |
| `vibe/cli-rust/src/cli.rs` | `--list-keys`, `--export-keys`, `--export-keys-json`, `--import-keys` CLI flags |
| `vibe/cli-rust/src/main.rs` | Key management flag handlers for Rust CLI |
| `vibe/cli-rust/src/credentials/mod.rs` | Added `api_keys` module to credentials |
| `vibe/core/paths/*` | `whoami_cache_multi.json` path |

### Rename `MISTRAL_API_KEY(S)` → `VIBE_API_KEY(S)` (commit `01ae2bd2`)
Touches ~70 files, mostly tests and docs. The one line that matters is
`DEFAULT_MISTRAL_API_ENV_KEY = "VIBE_API_KEY"` in `vibe/core/config/_defaults.py`.
After a sync, re-run: `git grep -l MISTRAL_API_KEY` and rename only what refers
to the Vibe sign-in key, leaving third-party providers on their own variables.

## Sync procedure
1. `git fetch upstream && git merge upstream/main` (or rebase our commits).
2. Resolve conflicts in the table above; our own files will not conflict.
3. Re-run the rename check above.
4. `python -m pytest tests/backend tests/cli tests/test_key_accounts.py`.
5. Native module: a test run may rebuild `mistralai_vibe_local_harness/_native.abi3.so`
   (`uv build` via maturin); wait for it to finish before judging results.
