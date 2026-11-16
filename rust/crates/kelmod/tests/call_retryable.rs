//! `kelmod call` against a fake daemon socket: a client error says it is retryable when nothing reached the
//! daemon before the call frame was written, or when the call carried a key; after that write an unkeyed call
//! may have run, and its error is not retryable.

use std::io::{Read, Write};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;

use serde_json::{Value, json};

static NEXT: AtomicUsize = AtomicUsize::new(0);

fn runtime_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "kr9-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("runtime dir");
    dir
}

#[derive(Clone, Copy)]
enum After {
    /// Closes the connection once the call arrived, without a reply.
    Close,
    /// Never replies; waits for the client to give up.
    Silent,
}

/// A daemon that answers the hello, reads the call, then closes or stays silent. Joins to the call's header.
fn fake_daemon(dir: &Path, after: After) -> thread::JoinHandle<Value> {
    let listener = UnixListener::bind(dir.join("d.sock")).expect("bind");
    thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept");
        let mut reader = kelmo_wire::FrameReader::new();
        let mut buffer = [0u8; 65536];
        let mut frames = Vec::new();
        let mut greeted = false;
        loop {
            let read = stream.read(&mut buffer).expect("read");
            if read == 0 {
                return frames
                    .get(1)
                    .map(|(header, _): &(Value, Value)| header.clone())
                    .unwrap_or(Value::Null);
            }
            frames.extend(reader.feed(&buffer[..read]).expect("frame"));
            if !greeted && !frames.is_empty() {
                let hello = json!({"v": 2, "kind": "hello", "id": "0123456789abcdef0123456789abcdef", "body_type": "json",
                                   "protocols": ["kelmo/2"], "operations": []});
                let bytes = kelmo_wire::encode(&hello, &Value::Null).expect("encode");
                stream.write_all(&bytes).expect("write");
                greeted = true;
            }
            if frames.len() >= 2 && matches!(after, After::Close) {
                return frames[1].0.clone();
            }
        }
    })
}

/// Runs `kelmod call archive.echo` with `extra` and returns its JSON error.
fn kelmod_call(dir: &Path, extra: &[&str]) -> Value {
    let output = Command::new(env!("CARGO_BIN_EXE_kelmod"))
        .args(["call", "archive.echo", "--runtime-dir"])
        .arg(dir)
        .args(extra)
        .output()
        .expect("kelmod runs");
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let answer: Value = serde_json::from_slice(&output.stdout).expect("JSON on stdout");
    answer["error"].clone()
}

#[test]
fn an_unkeyed_call_the_daemon_dropped_after_receiving_it_is_not_retryable() {
    let dir = runtime_dir();
    let daemon = fake_daemon(&dir, After::Close);
    let error = kelmod_call(&dir, &[]);
    let call = daemon.join().expect("daemon");
    assert_eq!(call["kind"], "call", "the daemon received the call");
    assert!(call.get("key").is_none());
    assert_eq!(error["code"], "E_TRANSPORT", "{error}");
    assert_eq!(error["retryable"], Value::Bool(false), "{error}");
}

#[test]
fn a_call_that_never_reached_a_socket_is_retryable() {
    let dir = runtime_dir();
    let error = kelmod_call(&dir, &[]);
    assert_eq!(error["code"], "E_TRANSPORT", "{error}");
    assert_eq!(error["retryable"], Value::Bool(true), "{error}");
}

#[test]
fn a_keyed_call_the_daemon_dropped_after_receiving_it_is_retryable() {
    let dir = runtime_dir();
    let daemon = fake_daemon(&dir, After::Close);
    let error = kelmod_call(&dir, &["--key", "note-1"]);
    let call = daemon.join().expect("daemon");
    assert_eq!(call["key"], "note-1");
    assert_eq!(error["code"], "E_TRANSPORT", "{error}");
    assert_eq!(error["retryable"], Value::Bool(true), "{error}");
}

#[test]
fn an_unkeyed_call_past_its_deadline_after_sending_is_not_retryable() {
    let dir = runtime_dir();
    let daemon = fake_daemon(&dir, After::Silent);
    let error = kelmod_call(&dir, &["--deadline-ms", "10"]);
    let call = daemon.join().expect("daemon");
    assert_eq!(call["kind"], "call", "the daemon received the call");
    assert_eq!(error["code"], "E_DEADLINE", "{error}");
    assert_eq!(error["retryable"], Value::Bool(false), "{error}");
}
