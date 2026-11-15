//! Rust CLI against the real Python app-server and a local mock Mistral API.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::process::Command;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

struct MockApi {
    address: String,
    seen: Arc<Mutex<Vec<String>>>,
    statuses: Arc<Mutex<Vec<u16>>>,
    tool_call: Arc<AtomicBool>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl MockApi {
    fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = format!("http://{}", listener.local_addr().unwrap());
        let seen = Arc::new(Mutex::new(Vec::new()));
        let statuses = Arc::new(Mutex::new(Vec::new()));
        let tool_call = Arc::new(AtomicBool::new(false));
        let stop = Arc::new(AtomicBool::new(false));
        let (seen_thread, statuses_thread, tool_thread, stop_thread) = (
            seen.clone(),
            statuses.clone(),
            tool_call.clone(),
            stop.clone(),
        );
        let thread = thread::spawn(move || {
            while !stop_thread.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((stream, _)) => serve(stream, &seen_thread, &statuses_thread, &tool_thread),
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("mock listener: {error}"),
                }
            }
        });
        Self {
            address,
            seen,
            statuses,
            tool_call,
            stop,
            thread: Some(thread),
        }
    }

    fn completion_keys(&self) -> Vec<String> {
        self.seen.lock().unwrap().clone()
    }
}

impl Drop for MockApi {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.thread.take().unwrap().join().unwrap();
    }
}

fn serve(
    mut stream: TcpStream,
    seen: &Mutex<Vec<String>>,
    statuses: &Mutex<Vec<u16>>,
    tool_call: &AtomicBool,
) {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut request = Vec::new();
    let mut chunk = [0; 8192];
    let headers_end = loop {
        let Ok(count) = stream.read(&mut chunk) else {
            return;
        };
        if count == 0 {
            return;
        }
        request.extend_from_slice(&chunk[..count]);
        if let Some(end) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
            let headers = String::from_utf8_lossy(&request[..end]);
            let length = headers
                .lines()
                .filter_map(|line| line.split_once(':'))
                .find(|(name, _)| name.eq_ignore_ascii_case("content-length"))
                .and_then(|(_, value)| value.trim().parse::<usize>().ok())
                .unwrap_or(0);
            if request.len() >= end + 4 + length {
                break end;
            }
        }
    };
    let headers = String::from_utf8_lossy(&request[..headers_end]);
    let path = headers.lines().next().unwrap_or_default();
    let key = headers
        .lines()
        .filter_map(|line| line.split_once(':'))
        .find(|(name, _)| name.eq_ignore_ascii_case("authorization"))
        .map(|(_, value)| value.trim().trim_start_matches("Bearer ").to_owned())
        .unwrap_or_default();
    let (status, content_type, body) = if path.contains("/chat/completions") {
        seen.lock().unwrap().push(key);
        let status = {
            let mut statuses = statuses.lock().unwrap();
            if statuses.is_empty() {
                200
            } else {
                statuses.remove(0)
            }
        };
        if status == 200 {
            let tool = tool_call.swap(false, Ordering::Relaxed);
            let delta = if tool {
                serde_json::json!({"role": "assistant", "tool_calls": [{
                    "index": 0, "id": "call_1", "type": "function",
                    "function": {"name": "bash", "arguments": "{\"command\":\"echo pool-test\"}"}
                }]})
            } else {
                serde_json::json!({"role": "assistant", "content": "hello"})
            };
            let chunk = |delta, finish| {
                serde_json::json!({
                    "id": "mock", "object": "chat.completion.chunk", "created": 1,
                    "model": "mock-model",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4}
                })
            };
            let finish = if tool { "tool_calls" } else { "stop" };
            let body = format!(
                "data: {}\n\ndata: {}\n\ndata: [DONE]\n\n",
                chunk(delta, serde_json::Value::Null),
                chunk(serde_json::json!({}), serde_json::json!(finish))
            );
            (200, "text/event-stream", body)
        } else {
            (
                status,
                "application/json",
                r#"{"message":"refused"}"#.to_owned(),
            )
        }
    } else {
        (404, "application/json", "{}".to_owned())
    };
    let response = format!(
        "HTTP/1.1 {status} OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(response.as_bytes()).unwrap();
}

fn configure(home: &Path, api: &MockApi) {
    std::fs::write(
        home.join("config.toml"),
        format!(
            r#"active_model = "mock-model"
enable_update_checks = false
enable_telemetry = false
enable_connectors = false
console_base_url = "{base}"
vibe_base_url = "{base}"
[[providers]]
name = "mistral"
api_base = "{base}/v1"
api_key_env_var = "VIBE_API_KEY"
backend = "mistral"
preserve_api_base = true
[[models]]
name = "mock-model"
provider = "mistral"
alias = "mock-model"
"#,
            base = api.address
        ),
    )
    .unwrap();
}

fn run(home: &Path, flags: &[&str]) -> std::process::Output {
    let project = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let server = project.join(".venv/bin/vibe-app-server");
    assert!(
        server.exists(),
        "run uv sync in the project before this test"
    );
    let keys = (0..10).map(|n| format!("test-key-{n}")).collect::<Vec<_>>();
    Command::new(env!("CARGO_BIN_EXE_vibe-rs"))
        .args(flags)
        .args(["-p", "hello", "--time-limit", "90"])
        .current_dir(home)
        .env("VIBE_HOME", home)
        .env("VIBE_API_KEY", &keys[0])
        .env("VIBE_API_KEYS", keys.join(","))
        .env("VIBE_APP_SERVER_BIN", server)
        .env("VIBE_TEST_DISABLE_KEYRING", "1")
        .env("VIBE_TEST_DISABLE_AUTO_TITLE", "1")
        .env("VIBE_TEST_DISABLE_MODEL_PROBE", "1")
        .env_remove("VIBE_APP_SERVER_CMD")
        .env_remove("VIBE_APP_SERVER_CWD")
        .env_remove("VIBE_POOL_STATIC")
        .env_remove("VIBE_POOL_AGRESSIVE_ROTATION")
        .env_remove("VIBE_CWD")
        .output()
        .unwrap()
}

#[test]
fn ten_key_pool_preserves_or_shuffles_all_keys() {
    let project = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    let sample = |static_pool: bool| {
        let output = Command::new("uv")
            .args([
                "run",
                "--no-sync",
                "python",
                "-c",
                "import json; from vibe.core.llm.key_pool import KeyPool; print(json.dumps([KeyPool([f'test-key-{i}' for i in range(10)]).keys for _ in range(6)]))",
            ])
            .current_dir(&project)
            .env("VIBE_POOL_STATIC", if static_pool { "1" } else { "" })
            .env_remove("VIBE_POOL_AGRESSIVE_ROTATION")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        serde_json::from_slice::<Vec<Vec<String>>>(&output.stdout).unwrap()
    };
    let expected = (0..10).map(|i| format!("test-key-{i}")).collect::<Vec<_>>();
    for keys in sample(true) {
        assert_eq!(keys, expected);
    }
    let shuffled = sample(false);
    assert!(shuffled.iter().any(|keys| keys != &expected));
    for mut keys in shuffled {
        keys.sort();
        assert_eq!(keys, expected);
    }
}

#[test]
fn static_pool_sends_first_key_to_real_mock_api() {
    let home = tempfile::tempdir().unwrap();
    let api = MockApi::new();
    configure(home.path(), &api);
    let output = run(home.path(), &["--static-pool"]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(String::from_utf8_lossy(&output.stdout).contains("hello"));
    assert_eq!(api.completion_keys(), ["test-key-0"]);
}

#[test]
fn rejected_keys_are_skipped_on_later_runs() {
    let home = tempfile::tempdir().unwrap();
    let api = MockApi::new();
    configure(home.path(), &api);
    *api.statuses.lock().unwrap() = vec![401, 403, 200, 200];
    let first = run(home.path(), &["--static-pool"]);
    assert!(
        !first.status.success() && api.completion_keys() == ["test-key-0"],
        "first rejection: {:?}, stdout: {}, stderr: {}",
        api.completion_keys(),
        String::from_utf8_lossy(&first.stdout),
        String::from_utf8_lossy(&first.stderr)
    );
    let second = run(home.path(), &["--static-pool"]);
    assert!(
        !second.status.success() && api.completion_keys() == ["test-key-0", "test-key-1"],
        "second rejection: {:?}, stdout: {}, stderr: {}",
        api.completion_keys(),
        String::from_utf8_lossy(&second.stdout),
        String::from_utf8_lossy(&second.stderr)
    );
    for _ in 0..2 {
        let output = run(home.path(), &["--static-pool"]);
        assert!(
            output.status.success(),
            "keys: {:?}, stdout: {}, stderr: {}",
            api.completion_keys(),
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(String::from_utf8_lossy(&output.stdout).contains("hello"));
    }
    assert_eq!(
        api.completion_keys(),
        ["test-key-0", "test-key-1", "test-key-2", "test-key-2"]
    );
}

#[test]
fn shuffled_pool_changes_the_initial_account_across_runs() {
    let home = tempfile::tempdir().unwrap();
    let api = MockApi::new();
    configure(home.path(), &api);
    for _ in 0..8 {
        let output = run(home.path(), &[]);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }

    let keys = api.completion_keys();
    assert_eq!(keys.len(), 8);
    assert!(keys.iter().any(|key| key != &keys[0]), "{keys:?}");
}

#[test]
fn rotation_modes_apply_to_multiple_model_calls_in_one_session() {
    for (flags, expected) in [
        (
            &["--static-pool", "--auto-approve"][..],
            ["test-key-0", "test-key-0"],
        ),
        (
            &["--static-pool", "--agressive-rotation", "--auto-approve"][..],
            ["test-key-0", "test-key-1"],
        ),
    ] {
        let home = tempfile::tempdir().unwrap();
        let api = MockApi::new();
        configure(home.path(), &api);
        api.tool_call.store(true, Ordering::Relaxed);
        let output = run(home.path(), flags);
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(String::from_utf8_lossy(&output.stdout).contains("hello"));
        assert_eq!(api.completion_keys(), expected);
    }
}
