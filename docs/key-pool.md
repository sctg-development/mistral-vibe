# Multi-account key pool

The fork supports multiple API keys in the provider's plural environment
variable, such as `VIBE_API_KEYS=key1,key2,key3`. Keys stored by sign-in are
also included. With at least two distinct keys, each pool is shuffled once
when initialized. The selected key is reused for subsequent model calls until
it is rejected, preserving the opportunity to reuse the prompt cache.

- `--static-pool` or a non-empty `VIBE_POOL_STATIC` preserves the configured
  key order instead of shuffling.
- `--agressive-rotation` or a non-empty `VIBE_POOL_AGRESSIVE_ROTATION` enables
  the previous aggressive spreading policy: select an account on each call,
  considering recent use, in-flight requests and available rate-limit headroom.

An empty environment variable disables its option; any non-empty value,
including `0`, enables it. Flags apply to both interactive and headless
`vibe` runs. The Python launcher passes them to the Rust CLI, which forwards
the options to its app server. Environment variables also apply to other
Python entry points, such as `vibe-acp`.

Use `vibe --static-pool --agressive-rotation` to restore the previous
deterministic aggressive behavior. Cooldowns are shared between processes in
`$VIBE_HOME/key_pool_state.json`, but the selected key and shuffled order are
local to each pool.

For the CLI's API-key credentials, `/whoami` reads the session's selected
pooled account and refreshes it when invoked. Account reads do not themselves
rotate the pool. Desktop-supplied credentials and CLI OIDC sessions retain
their own credential selection and are not replaced with pooled keys.

On the Unified harness path, only 401 and 403 credential rejections are
reported to the pool. In particular, 429 rate-limit errors are not reported
to it, so they cannot trigger pool failover there.
