//! Exit codes of argument parsing: a bad argument is a usage error.

use std::process::Command;

fn run(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_vibe-rs"))
        .args(args)
        .env_remove("VIBE_CWD")
        .output()
        .unwrap()
}

#[test]
fn a_bad_argument_exits_one_with_the_parse_error() {
    for args in [
        &["--max-turns", "many", "-p", "hi"][..],
        &["--no-such-flag"][..],
        &["-c", "--resume", "abc"][..],
    ] {
        let output = run(args);
        assert_eq!(output.status.code(), Some(1), "{args:?}");
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains("error:"), "{args:?}: {stderr}");
    }
}

#[test]
fn help_and_version_exit_zero() {
    for args in [&["--help"][..], &["--version"][..]] {
        let output = run(args);
        assert_eq!(output.status.code(), Some(0), "{args:?}");
        assert!(!output.stdout.is_empty(), "{args:?}");
    }
}

#[cfg(unix)]
#[test]
fn pool_flags_reach_the_app_server_even_with_dotenv_values() {
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join(".env"),
        "VIBE_POOL_STATIC=from-file\nVIBE_POOL_AGRESSIVE_ROTATION=from-file\n",
    )
    .unwrap();
    let script = dir.path().join("server.sh");
    std::fs::write(
        &script,
        "printf '%s\\n%s\\n' \"$VIBE_POOL_STATIC\" \"$VIBE_POOL_AGRESSIVE_ROTATION\" > pool-env\n",
    )
    .unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_vibe-rs"))
        .args(["--static-pool", "--agressive-rotation", "-p", "hello"])
        .current_dir(dir.path())
        .env("VIBE_HOME", dir.path())
        .env("VIBE_APP_SERVER_CMD", format!("sh '{}'", script.display()))
        .env_remove("VIBE_POOL_STATIC")
        .env_remove("VIBE_POOL_AGRESSIVE_ROTATION")
        .env_remove("VIBE_CWD")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert_eq!(
        std::fs::read_to_string(dir.path().join("pool-env")).unwrap(),
        "1\n1\n"
    );
}
