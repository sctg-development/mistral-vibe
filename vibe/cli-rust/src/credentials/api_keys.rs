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

const CONSOLE_BASE_URL: &str = "https://console.mistral.ai";
const API_BASE_URL: &str = "https://api.mistral.ai/v1";
const LOOKUP_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(10);
const IDENTITY_CONCURRENCY: usize = 3;
const IDENTITY_MAX_RETRIES: u32 = 3;

/// Account details for one key (Python `WhoAmIResult` + `IdentityResult`).
#[derive(Default)]
struct KeyAccount {
    whoami: Option<serde_json::Value>,
    identity: Option<serde_json::Value>,
    /// Whether a lookup went to the network (the cache needs an update).
    fetched: bool,
}

/// `GET {url}` with the bearer key. `Ok(None)` is a 401/403 (rejected key),
/// `Err` a transient failure worth retrying.
async fn get_json(
    client: &reqwest::Client,
    url: &str,
    api_key: &str,
) -> Result<Option<serde_json::Value>, ()> {
    let response = client
        .get(url)
        .header("Authorization", format!("Bearer {api_key}"))
        .send()
        .await
        .map_err(|_| ())?;
    let status = response.status();
    if status == reqwest::StatusCode::UNAUTHORIZED || status == reqwest::StatusCode::FORBIDDEN {
        return Ok(None);
    }
    if !status.is_success() {
        return Err(());
    }
    response.json().await.map(Some).map_err(|_| ())
}

async fn lookup_account(
    client: reqwest::Client,
    semaphore: std::sync::Arc<tokio::sync::Semaphore>,
    api_key: String,
    cached: KeyAccount,
) -> KeyAccount {
    let whoami_url = format!("{CONSOLE_BASE_URL}/api/vibe/whoami");
    let identity_url = format!("{API_BASE_URL}/users/me");
    let whoami = async {
        match cached.whoami.clone() {
            Some(value) => (Some(value), false),
            None => (
                get_json(&client, &whoami_url, &api_key)
                    .await
                    .ok()
                    .flatten(),
                true,
            ),
        }
    };
    let identity = async {
        if let Some(value) = cached.identity.clone() {
            return (Some(value), false);
        }
        let Ok(_permit) = semaphore.acquire().await else {
            return (None, true);
        };
        for attempt in 0..IDENTITY_MAX_RETRIES {
            match get_json(&client, &identity_url, &api_key).await {
                Ok(value) => return (value, true),
                Err(()) if attempt + 1 < IDENTITY_MAX_RETRIES => {
                    tokio::time::sleep(std::time::Duration::from_secs(1 << attempt)).await;
                }
                Err(()) => {}
            }
        }
        (None, true)
    };
    let ((whoami, who_fetched), (identity, id_fetched)) = tokio::join!(whoami, identity);
    KeyAccount {
        whoami,
        identity,
        fetched: who_fetched || id_fetched,
    }
}

fn describe_account(account: &KeyAccount) -> String {
    if account.whoami.is_none() && account.identity.is_none() {
        return "key rejected or console unreachable".to_owned();
    }
    let mut details: Vec<String> = Vec::new();
    if let Some(who) = &account.whoami {
        let text = |field: &str| {
            who.get(field)
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
        };
        details.push(format!(
            "account={}",
            text("customer_id").unwrap_or("unknown")
        ));
        details.push(format!("plan={}", text("plan_name").unwrap_or("unknown")));
        if let Some(org) = text("organization_kind") {
            details.push(format!("org={org}"));
        }
    }
    let email = account
        .identity
        .as_ref()
        .and_then(|v| v.get("email"))
        .and_then(|v| v.as_str())
        .filter(|e| !e.is_empty());
    if let Some(email) = email {
        details.push(format!("email={email}"));
    }
    details.join(" ")
}

const CACHE_FILE_NAME: &str = "whoami_cache_multi.json";
const CACHE_TTL_SECONDS: u64 = 6 * 60 * 60;

type CacheEntries = Vec<serde_json::Value>;

fn now_seconds() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Python `hash_api_key`: first 32 hex chars of the SHA-256 of the key.
fn hash_api_key(api_key: &str) -> String {
    use sha2::{Digest, Sha256};
    let digest = Sha256::digest(api_key.as_bytes());
    digest.iter().take(16).map(|b| format!("{b:02x}")).collect()
}

fn cache_path() -> Option<std::path::PathBuf> {
    crate::utils::paths::vibe_home().map(|home| home.join(CACHE_FILE_NAME))
}

/// Shares `$VIBE_HOME/whoami_cache_multi.json` with Python `list_keys.py`.
fn read_cache() -> CacheEntries {
    let Some(path) = cache_path() else {
        return Vec::new();
    };
    std::fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
        .and_then(|value| match value {
            serde_json::Value::Array(entries) => Some(entries),
            _ => None,
        })
        .unwrap_or_default()
}

fn write_cache(entries: &CacheEntries) {
    let Some(path) = cache_path() else {
        return;
    };
    let tmp = path.with_file_name(format!(".{CACHE_FILE_NAME}.{}.tmp", std::process::id()));
    let written = path
        .parent()
        .map(std::fs::create_dir_all)
        .transpose()
        .ok()
        .and_then(|_| serde_json::to_vec(entries).ok())
        .map(|bytes| std::fs::write(&tmp, bytes).and_then(|_| std::fs::rename(&tmp, &path)));
    if !matches!(written, Some(Ok(()))) {
        let _ = std::fs::remove_file(&tmp);
    }
}

/// The fresh cached lookups for `api_key`; a missing half is fetched again.
fn cached_account(entries: &CacheEntries, api_key: &str) -> KeyAccount {
    let key_hash = hash_api_key(api_key);
    let floor = now_seconds().saturating_sub(CACHE_TTL_SECONDS);
    let Some(entry) = entries
        .iter()
        .find(|e| e.get("key_hash").and_then(|v| v.as_str()) == Some(key_hash.as_str()))
        .filter(|e| e.get("stored_at_timestamp").and_then(|v| v.as_u64()) > Some(floor))
    else {
        return KeyAccount::default();
    };
    let payload = |field: &str| entry.get(field).filter(|v| v.is_object()).cloned();
    KeyAccount {
        whoami: payload("whoami"),
        identity: payload("identity"),
        fetched: false,
    }
}

fn store_account(entries: &mut CacheEntries, api_key: &str, account: &KeyAccount) {
    let key_hash = hash_api_key(api_key);
    let position = entries
        .iter()
        .position(|e| e.get("key_hash").and_then(|v| v.as_str()) == Some(key_hash.as_str()));
    let mut entry = position
        .map(|i| entries.remove(i))
        .unwrap_or_else(|| serde_json::json!({ "key_hash": key_hash }));
    if let Some(object) = entry.as_object_mut() {
        if let Some(whoami) = &account.whoami {
            object.insert("whoami".into(), whoami.clone());
        }
        if let Some(identity) = &account.identity {
            object.insert("identity".into(), identity.clone());
        }
        object.insert("stored_at_timestamp".into(), now_seconds().into());
    }
    entries.push(entry);
}

/// Run --list-keys: print pooled keys with account info, then exit.
pub async fn run_list_keys() -> Result<std::process::ExitCode> {
    let keys = pooled_keys();
    if keys.is_empty() {
        process::exit(1);
    }

    let client = crate::update_notifier::gateway::wizard_http_client(LOOKUP_TIMEOUT, false)
        .context("Can't build HTTP client")?;
    let semaphore = std::sync::Arc::new(tokio::sync::Semaphore::new(IDENTITY_CONCURRENCY));
    let mut cache = read_cache();
    let handles: Vec<_> = keys
        .iter()
        .map(|key| {
            tokio::spawn(lookup_account(
                client.clone(),
                semaphore.clone(),
                key.clone(),
                cached_account(&cache, key),
            ))
        })
        .collect();
    let mut cache_dirty = false;

    let mut first_by_account: std::collections::HashMap<String, usize> = Default::default();
    for (index, (key, handle)) in keys.iter().zip(handles).enumerate() {
        let account = handle.await.unwrap_or_default();
        if account.fetched {
            store_account(&mut cache, key, &account);
            cache_dirty = true;
        }
        let position = index + 1;
        let mut line = format!("{key}  {}", describe_account(&account));
        if let Some(customer_id) = account
            .whoami
            .as_ref()
            .and_then(|w| w.get("customer_id"))
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
        {
            let first = *first_by_account
                .entry(customer_id.to_owned())
                .or_insert(position);
            if first != position {
                line.push_str(&format!("  DUPLICATE of #{first}"));
            }
        }
        println!("{line}");
    }
    if cache_dirty {
        write_cache(&cache);
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
            .args([
                "delete-generic-password",
                "-s",
                KEYRING_SERVICE,
                "-a",
                env_key,
            ])
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
        if api_key.is_empty() {
            // Nothing to do for empty keys on non-macOS
            return Ok(());
        }

        let service = KEYRING_SERVICE.to_owned();
        let username = env_key.to_owned();
        let password = api_key.to_owned();

        // Use a thread for the blocking keyring call
        on_keyring_thread(move || {
            let entry = ::keyring::Entry::new(&service, &username);
            match entry {
                Ok(entry) => entry
                    .set_password(&password)
                    .map_err(|e| format!("keyring write failed: {}", e)),
                Err(e) => Err(format!("keyring entry create failed: {}", e)),
            }
        })
        .map_err(|e| anyhow::anyhow!(e))?;
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
pub async fn run_import_keys(raw_keys: &str) -> Result<std::process::ExitCode> {
    let pool_var = pool_env_var(DEFAULT_MISTRAL_API_ENV_KEY);

    // Keys given on the command line, or stdin when omitted or `-`.
    let mut input = String::new();
    if raw_keys == "-" {
        io::stdin()
            .read_to_string(&mut input)
            .context("Error reading input")?;
    } else {
        input = raw_keys.to_owned();
    }

    let keys = split_keys(&input);

    // Store in keyring
    if !keys.is_empty() {
        let joined = keys.join(",");
        set_api_key_in_keyring(&pool_var, &joined).context("Error importing keys")?;
        println!(
            "Successfully imported {} key(s) into the keyring.",
            keys.len()
        );
    } else {
        // Clear keys by setting empty string
        set_api_key_in_keyring(&pool_var, "").context("Error clearing keys")?;
        println!("Cleared all keys from the keyring.");
    }

    Ok(process::ExitCode::from(0))
}
