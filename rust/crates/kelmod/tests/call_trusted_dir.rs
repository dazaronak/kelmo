//! `kelmod call` connects only through a runtime directory this user owns and nobody else can write into, as the
//! gateway does: a directory open to others, or a symlink, is refused before anything is sent.

use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use serde_json::Value;

static NEXT: AtomicUsize = AtomicUsize::new(0);

fn fresh(mode: u32) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "ktd-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("dir");
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(mode)).expect("mode");
    dir
}

/// Calls kelmod through `dir` and returns its JSON error.
fn call_through(dir: &Path) -> Value {
    let output = Command::new(env!("CARGO_BIN_EXE_kelmod"))
        .args(["call", "archive.echo", "--runtime-dir"])
        .arg(dir)
        .args(["--deadline-ms", "1000"])
        .output()
        .expect("kelmod runs");
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let answer: Value = serde_json::from_slice(&output.stdout).expect("JSON on stdout");
    answer["error"].clone()
}

/// A listener on `dir/d.sock`; returns whether anyone connected within a second after kelmod ran.
fn listening(dir: &Path) -> UnixListener {
    let listener = UnixListener::bind(dir.join("d.sock")).expect("bind");
    listener.set_nonblocking(true).expect("nonblocking");
    listener
}

fn connected(listener: &UnixListener) -> bool {
    std::thread::sleep(Duration::from_millis(200));
    listener.accept().is_ok()
}

#[test]
fn a_runtime_directory_open_to_other_users_is_refused_before_connecting() {
    let dir = fresh(0o755);
    let listener = listening(&dir);
    let error = call_through(&dir);
    assert_eq!(error["code"], "E_RUNTIME", "{error}");
    assert_eq!(error["retryable"], Value::Bool(false), "{error}");
    assert!(
        !connected(&listener),
        "kelmod connected through an untrusted directory"
    );
}

#[test]
fn a_runtime_directory_that_is_a_symlink_is_refused_before_connecting() {
    let real = fresh(0o700);
    let listener = listening(&real);
    let link = real.with_extension("link");
    let _ = std::fs::remove_file(&link);
    std::os::unix::fs::symlink(&real, &link).expect("symlink");
    let error = call_through(&link);
    assert_eq!(error["code"], "E_RUNTIME", "{error}");
    assert_eq!(error["retryable"], Value::Bool(false), "{error}");
    assert!(
        !connected(&listener),
        "kelmod connected through a symlinked directory"
    );
}

#[test]
fn a_missing_runtime_directory_is_still_a_retryable_transport_error() {
    let dir = fresh(0o700).join("absent");
    let error = call_through(&dir);
    assert_eq!(error["code"], "E_TRANSPORT", "{error}");
    assert_eq!(error["retryable"], Value::Bool(true), "{error}");
}

#[test]
fn an_empty_runtime_directory_never_reaches_a_socket_in_the_working_directory() {
    // `--runtime-dir ''` names the relative `d.sock`: one planted in the working directory must not get the call.
    let cwd = fresh(0o755);
    let listener = listening(&cwd);
    let output = Command::new(env!("CARGO_BIN_EXE_kelmod"))
        .args([
            "call",
            "archive.echo",
            "--runtime-dir",
            "",
            "--deadline-ms",
            "1000",
        ])
        .current_dir(&cwd)
        .output()
        .expect("kelmod runs");
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let answer: Value = serde_json::from_slice(&output.stdout).expect("JSON on stdout");
    let error = &answer["error"];
    assert_eq!(error["code"], "E_TRANSPORT", "{error}");
    assert_eq!(error["retryable"], Value::Bool(true), "{error}");
    assert!(
        !connected(&listener),
        "kelmod connected to a socket in the working directory"
    );
}
