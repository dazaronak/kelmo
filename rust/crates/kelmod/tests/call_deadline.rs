//! `kelmod call --deadline-ms` bounds the whole call: a daemon that never answers the hello, or never reads the
//! call frame, makes kelmod end with `E_DEADLINE` instead of waiting with no output.

use std::io::{Read, Write};
use std::os::unix::net::UnixListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

static NEXT: AtomicUsize = AtomicUsize::new(0);

fn runtime_dir() -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "kdl-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("runtime dir");
    std::fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .expect("runtime dir mode");
    dir
}

/// A daemon that accepts one connection, answers the hello only when `greet`, then holds the connection open
/// without reading anything more for 30 s.
fn stalled_daemon(dir: &Path, greet: bool) {
    let listener = UnixListener::bind(dir.join("d.sock")).expect("bind");
    thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept");
        if greet {
            let mut reader = kelmo_wire::FrameReader::new();
            let mut buffer = [0u8; 4096];
            loop {
                let read = stream.read(&mut buffer).expect("read");
                if read == 0 || !reader.feed(&buffer[..read]).expect("frame").is_empty() {
                    break;
                }
            }
            let hello = json!({"v": 2, "kind": "hello", "id": "0123456789abcdef0123456789abcdef",
                               "body_type": "json", "protocols": ["kelmo/2"], "operations": []});
            stream
                .write_all(&kelmo_wire::encode(&hello, &Value::Null).expect("encode"))
                .expect("write");
        }
        thread::sleep(Duration::from_secs(30));
        drop(stream);
    });
}

/// Runs `kelmod call archive.echo --deadline-ms 10` with `extra`; returns its JSON error and how long it ran.
fn bounded_call(dir: &Path, extra: &[&str]) -> (Value, Duration) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_kelmod"))
        .args(["call", "archive.echo", "--runtime-dir"])
        .arg(dir)
        .args(["--deadline-ms", "10"])
        .args(extra)
        .stdout(Stdio::piped())
        .spawn()
        .expect("kelmod runs");
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(10) {
        if child.try_wait().expect("wait").is_some() {
            break;
        }
        thread::sleep(Duration::from_millis(50));
    }
    let elapsed = start.elapsed();
    if child.try_wait().expect("wait").is_none() {
        let _ = child.kill();
        let _ = child.wait();
        panic!("kelmod still waits {elapsed:?} after a 10 ms deadline");
    }
    let mut stdout = String::new();
    child
        .stdout
        .take()
        .expect("stdout")
        .read_to_string(&mut stdout)
        .expect("read stdout");
    let answer: Value = serde_json::from_str(&stdout).expect("JSON on stdout");
    (answer["error"].clone(), elapsed)
}

#[test]
fn a_daemon_that_never_answers_the_hello_ends_the_call_at_its_deadline() {
    let dir = runtime_dir();
    stalled_daemon(&dir, false);
    let (error, elapsed) = bounded_call(&dir, &[]);
    assert!(elapsed < Duration::from_secs(10), "{elapsed:?}");
    assert_eq!(error["code"], "E_DEADLINE", "{error}");
    assert_eq!(error["retryable"], Value::Bool(true), "{error}");
}

#[test]
fn a_daemon_that_never_reads_the_call_ends_it_at_its_deadline() {
    let dir = runtime_dir();
    stalled_daemon(&dir, true);
    // Under Linux's 128 KiB limit for one argument; over the 8 KiB socket buffer of macOS, where the write
    // itself blocks. A buffer that takes it all ends at the reply wait with the same answer.
    let body = json!({"text": "x".repeat(120 * 1024)}).to_string();
    let (error, elapsed) = bounded_call(&dir, &["--body", &body]);
    assert!(elapsed < Duration::from_secs(10), "{elapsed:?}");
    assert_eq!(error["code"], "E_DEADLINE", "{error}");
    assert_eq!(error["retryable"], Value::Bool(false), "{error}");
}

#[test]
fn the_largest_deadline_is_an_answer_not_a_panic() {
    // `--deadline-ms` takes any u64; a deadline past what the clock can hold must not crash kelmod.
    let dir = runtime_dir().join("absent");
    let output = Command::new(env!("CARGO_BIN_EXE_kelmod"))
        .args(["call", "archive.echo", "--runtime-dir"])
        .arg(&dir)
        .args(["--deadline-ms", "18446744073709551615"])
        .output()
        .expect("kelmod runs");
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let answer: Value = serde_json::from_slice(&output.stdout).expect("JSON on stdout");
    assert_eq!(answer["error"]["code"], "E_TRANSPORT", "{answer}");
}

#[test]
fn a_slow_hello_does_not_take_the_time_the_reply_was_given() {
    // The window is 10 ms + 5 s. The hello takes 3 s and the reply 3 s more: 6 s from the start, 3 s from the
    // write of the call, which is where the daemon starts counting its deadline.
    let dir = runtime_dir();
    let listener = UnixListener::bind(dir.join("d.sock")).expect("bind");
    thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept");
        let mut reader = kelmo_wire::FrameReader::new();
        let mut buffer = [0u8; 65536];
        let mut frames: Vec<(Value, Value)> = Vec::new();
        let mut greeted = false;
        loop {
            let read = stream.read(&mut buffer).expect("read");
            if read == 0 {
                return;
            }
            frames.extend(reader.feed(&buffer[..read]).expect("frame"));
            if !greeted && !frames.is_empty() {
                thread::sleep(Duration::from_secs(3));
                let hello = json!({"v": 2, "kind": "hello", "id": "0123456789abcdef0123456789abcdef",
                                   "body_type": "json", "protocols": ["kelmo/2"], "operations": []});
                stream
                    .write_all(&kelmo_wire::encode(&hello, &Value::Null).expect("encode"))
                    .expect("write");
                greeted = true;
            }
            if frames.len() >= 2 {
                thread::sleep(Duration::from_secs(3));
                let id = frames[1].0["id"].as_str().expect("call id").to_owned();
                let reply = kelmo_wire::header::reply(&id, None);
                stream
                    .write_all(&kelmo_wire::encode(&reply, &json!({"echo": 1})).expect("encode"))
                    .expect("write");
                thread::sleep(Duration::from_secs(1));
                return;
            }
        }
    });
    let output = Command::new(env!("CARGO_BIN_EXE_kelmod"))
        .args(["call", "archive.echo", "--runtime-dir"])
        .arg(&dir)
        .args(["--deadline-ms", "10"])
        .output()
        .expect("kelmod runs");
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let answer: Value = serde_json::from_slice(&output.stdout).expect("JSON on stdout");
    assert_eq!(answer, json!({"echo": 1}), "{answer}");
}
