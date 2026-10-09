//! API key management: list, export, and import keys for the multi-account key pool.
//! Mirrors Python `vibe/cli/list_keys.py` and `vibe/cli/cli.py` key management flags.

use anyhow::{Context, Result};
use std::io::{self, Read};
use std::process;

use super::keyring;

const DEFAULT_MISTRAL_API_ENV_KEY: &str = "VIBE_API_KEY";
const POOL_ENV_SUFFIX: &str = "S";

/// Pool environment variable name for the given API key environment variable.
fn pool_env_var(api_key_env_var: &str) -> String {
    if api_key_env_var.is_empty() {
        String::new()
    } else {
        format!("{}{}", api_key_env_var, POOL_ENV_SUFFIX)
    }
}

/// Split keys on comma, semicolon, or newline, dropping empty strings.
fn split_keys(raw: &str) -> Vec<String> {
    if raw.is_empty() {
        return Vec::new();
    }
    raw.split(|c| c == ',' || c == ';' || c == '\n')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Read pooled keys from the environment variable and the keyring.
fn pooled_keys() -> Vec<String> {
    let pool_var = pool_env_var(DEFAULT_MISTRAL_API_ENV_KEY);
    let mut keys: Vec<String> = std::env::var(&pool_var)
        .ok()
        .map(|v| split_keys(&v))
        .unwrap_or_default();

    // Add keys from keyring (stored under the plural env var name)
    if let Some(stored) = keyring::get_api_key_from_keyring(&pool_var) {
        keys.extend(split_keys(&stored));
    }

    // Remove duplicates while preserving order
    let mut seen = std::collections::HashSet::new();
    keys.into_iter()
        .filter(|k| seen.insert(k.clone()))
        .collect()
}

/// Run --list-keys: print pooled keys with account info, then exit.
pub async fn run_list_keys() -> Result<std::process::ExitCode> {
    let keys = pooled_keys();

    // For now, just print the keys (we don't have the account lookup in Rust yet)
    // This mirrors the Python behavior but without the whoami calls
    for key in &keys {
        println!("{}", key);
    }

    if keys.is_empty() {
        process::exit(1);
    }
    Ok(process::ExitCode::from(0))
}

/// Run --export-keys: print pooled keys comma-separated, then exit.
pub async fn run_export_keys() -> Result<std::process::ExitCode> {
    let keys = pooled_keys();
    println!("{}", keys.join(","));

    if keys.is_empty() {
        process::exit(1);
    }
    Ok(process::ExitCode::from(0))
}

/// Run --export-keys-json: print pooled keys as structured JSON, then exit.
pub async fn run_export_keys_json() -> Result<std::process::ExitCode> {
    let keys = pooled_keys();

    // Build a simple JSON output with keys
    // For now, just include the keys array without the full account info
    // (which would require HTTP calls to Mistral's API)
    println!("{{ \"vibe\": {{ \"protocol\": \"vibe\", \"endpoint\": \"https://api.mistral.ai/v1/chat/completions\", \"userAgent\": \"mistral-client-rust/{}\", \"keys\": [{}] }} }}", 
        env!("CARGO_PKG_VERSION"),
        keys.iter().map(|k| format!("\"{k}\"")).collect::<Vec<_>>().join(",")
    );

    if keys.is_empty() {
        process::exit(1);
    }
    Ok(process::ExitCode::from(0))
}

/// Set API key in the OS keyring.
/// Mirrors Python `vibe/utils/keyring::set_api_key_in_keyring`.
/// Uses constant service "ai.mistral.vibe" and username is the env_key.
fn set_api_key_in_keyring(env_key: &str, api_key: &str) -> Result<()> {
    const KEYRING_SERVICE: &str = "ai.mistral.vibe";

    // On macOS we shell out to `security`
    #[cfg(target_os = "macos")]
    {
        // Delete existing password first
        let _ = std::process::Command::new("/usr/bin/security")
            .args(["delete-generic-password", "-s", KEYRING_SERVICE, "-a", env_key])
            .output();

        // Add new password (empty api_key means delete)
        if !api_key.is_empty() {
            let status = std::process::Command::new("/usr/bin/security")
                .args([
                    "add-generic-password",
                    "-s",
                    KEYRING_SERVICE,
                    "-a",
                    env_key,
                    "-w",
                    api_key,
                    "-A",
                ])
                .status()
                .with_context(|| "Can't store password in macOS Keychain")?;

            if !status.success() {
                anyhow::bail!("Can't store password in macOS Keychain");
            }
        }
    }

    // On non-macOS platforms (Linux, Windows, etc.) we use the keyring crate
    #[cfg(not(target_os = "macos"))]
    {
        use std::thread;

        if api_key.is_empty() {
            // Nothing to do for empty keys on non-macOS
            return Ok(());
        }

        let service = KEYRING_SERVICE.to_owned();
        let username = env_key.to_owned();
        let password = api_key.to_owned();

        // Use a thread for the blocking keyring call
        on_keyring_thread(move || {
            let entry = keyring::Entry::new(&service, &username);
            match entry {
                Ok(mut entry) => {
                    entry
                        .set_password(&password)
                        .map_err(|e| format!("keyring write failed: {}", e))
                }
                Err(e) => Err(format!("keyring entry create failed: {}", e)),
            }
        })
        .context("keyring write")??;
    }

    Ok(())
}

/// Run a keyring call on a dedicated thread: the Secret Service round-trip
/// blocks its thread, and callers sit inside the async runtime.
/// Copied from keyring.rs to avoid circular dependencies.
#[cfg(not(target_os = "macos"))]
fn on_keyring_thread<T, F>(f: F) -> Result<T, String>
where
    F: FnOnce() -> Result<T, String> + Send + 'static,
    T: Send + 'static,
{
    std::thread::Builder::new()
        .name("vibe-keyring-write".into())
        .spawn(f)
        .map_err(|e| format!("can't start keyring thread: {e}"))?
        .join()
        .map_err(|_| "keyring thread panicked".to_string())?
}

/// Run --import-keys: read keys from stdin and store in keyring, then exit.
pub async fn run_import_keys() -> Result<std::process::ExitCode> {
    let pool_var = pool_env_var(DEFAULT_MISTRAL_API_ENV_KEY);

    // Read from stdin
    let mut input = String::new();
    io::stdin()
        .read_to_string(&mut input)
        .context("Error reading input")?;

    let keys = split_keys(&input);

    // Store in keyring
    if !keys.is_empty() {
        let joined = keys.join(",");
        set_api_key_in_keyring(&pool_var, &joined)
            .context("Error importing keys")?;
        println!("Successfully imported {} key(s) into the keyring.", keys.len());
    } else {
        // Clear keys by setting empty string
        set_api_key_in_keyring(&pool_var, "")
            .context("Error clearing keys")?;
        println!("Cleared all keys from the keyring.");
    }

    Ok(process::ExitCode::from(0))
}
