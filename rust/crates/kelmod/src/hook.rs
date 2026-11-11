//! `kelmod hook ACTION [--provider P] (--root DIR | --runtime-dir DIR) [--state DIR]`: one native lifecycle
//! event. It reads the hook payload from stdin and sends it with what only the hook's process can see (the
//! working directory, the provider, the `KELMO_INTERNAL_JOB` guard) as `edge.hook`, or `state.hook` on a
//! daemon that serves no edge. The reply, when it has content, is printed for the agent.
//!
//! A hook never blocks a session and never loses a capture: when no daemon answers within 300 ms a capture
//! frame is written to `<state>/hook-spool/` (fsynced) for the daemon to ingest when it starts, nothing is
//! printed, and the exit status is 0. A SessionStart or prompt hook is not spooled (its context is for now);
//! it prints a notice that no pages were delivered. Every other failure is reported on stderr, status 0.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::Duration;

use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

/// The hook payload bound, the same as the CLI's.
const MAX_PAYLOAD: usize = 1_048_576;
const CONNECT_TIMEOUT: Duration = Duration::from_millis(300);
/// The longest a hook call may take in the daemon: a SessionStart hook bounds itself to 15 s, and the
/// providers give a hook 20 s.
const CALL_DEADLINE: Duration = Duration::from_secs(18);
/// A capture hook runs under a 10 s provider timeout and only queues: it gives up well before that.
const CAPTURE_DEADLINE: Duration = Duration::from_secs(7);

pub struct Options {
    pub action: String,
    pub provider: Option<String>,
    /// None when the runtime directory could not be derived: the hook then behaves as with no daemon.
    pub socket: Option<PathBuf>,
    pub state: Option<PathBuf>,
}

fn read_payload() -> Result<Value, String> {
    let mut raw = Vec::new();
    std::io::stdin()
        .take(MAX_PAYLOAD as u64 + 1)
        .read_to_end(&mut raw)
        .map_err(|e| format!("E_INPUT: hook input: {e}"))?;
    if raw.len() > MAX_PAYLOAD {
        return Err("E_INPUT: Hook input exceeds size limit.".into());
    }
    if raw.iter().all(u8::is_ascii_whitespace) {
        return Ok(json!({}));
    }
    serde_json::from_slice(&raw).map_err(|e| format!("E_INPUT: hook input is not JSON: {e}"))
}

/// Writes the frame as a visible spool entry for the daemon to ingest.
fn spool(state: &Path, frame: &Value) -> std::io::Result<PathBuf> {
    let (hidden, visible) = spool_inflight(state, frame)?;
    std::fs::rename(&hidden, &visible)?;
    Ok(visible)
}

/// Writes the frame under `<state>/hook-spool/` as a hidden in-flight file (fsynced, by rename so no reader sees
/// half of it); returns it and the visible name it takes if the call does not settle it. The daemon also takes
/// in-flight files a killed hook left behind, once they are a minute old.
fn spool_inflight(state: &Path, frame: &Value) -> std::io::Result<(PathBuf, PathBuf)> {
    use std::io::Write;
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    let directory = state.join("hook-spool");
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&directory)?;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or_default();
    let name = format!("{nanos:020}-{}.json", std::process::id());
    let temporary = directory.join(format!(".{name}.tmp"));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(frame.to_string().as_bytes())?;
    file.sync_all()?;
    let hidden = directory.join(format!(".inflight-{name}"));
    std::fs::rename(&temporary, &hidden)?;
    std::fs::File::open(&directory)?.sync_all()?;
    Ok((hidden, directory.join(name)))
}

async fn frames(
    stream: &mut UnixStream,
    reader: &mut kelmo_wire::FrameReader,
) -> Option<(Value, Value)> {
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let count = stream.read(&mut buffer).await.ok()?;
        if count == 0 {
            return None;
        }
        let mut decoded = reader.feed(&buffer[..count]).ok()?;
        if !decoded.is_empty() {
            return Some(decoded.remove(0));
        }
    }
}

async fn send(stream: &mut UnixStream, header: &Value, body: &Value) -> bool {
    match kelmo_wire::encode(header, body) {
        Ok(bytes) => stream.write_all(&bytes).await.is_ok(),
        Err(_) => false,
    }
}

/// Connects and says hello within the connect bound; None when no daemon answers in time.
async fn reach(socket: &Path) -> Option<(UnixStream, kelmo_wire::FrameReader, Value)> {
    // A runtime directory another user could have planted is treated as no daemon at all.
    kelmo_runtime::ipc::trusted_directory(socket.parent()?).ok()?;
    tokio::time::timeout(CONNECT_TIMEOUT, async {
        let mut stream = UnixStream::connect(socket).await.ok()?;
        let mut reader = kelmo_wire::FrameReader::new();
        let hello = json!({"v": 2, "kind": "hello", "id": format!("{:032x}", std::process::id()),
                           "body_type": "json", "protocols": ["kelmo/2"], "operations": []});
        if !send(&mut stream, &hello, &Value::Null).await {
            return None;
        }
        let (header, _) = frames(&mut stream, &mut reader).await?;
        Some((stream, reader, header))
    })
    .await
    .ok()
    .flatten()
}

/// The hook event whose output is context for the session now, never worth spooling.
fn context_event(action: &str) -> Option<&'static str> {
    match action {
        "start" => Some("SessionStart"),
        "prompt" => Some("UserPromptSubmit"),
        _ => None,
    }
}

pub async fn run(options: Options) -> i32 {
    let payload = match read_payload() {
        Ok(payload) => payload,
        Err(message) => {
            eprintln!("{message}");
            return 0;
        }
    };
    let context = json!({
        "provider": options.provider,
        "cwd": std::env::current_dir().ok().map(|p| p.display().to_string()),
        "internal_job": std::env::var("KELMO_INTERNAL_JOB").as_deref() == Ok("1"),
    });
    let body = json!({"action": options.action, "payload": payload, "context": context});
    let reached = match &options.socket {
        Some(socket) => reach(socket).await,
        None => None,
    };
    let Some((mut stream, mut reader, hello)) = reached else {
        if let Some(event) = context_event(&options.action) {
            // Context delivered later is worth nothing: nothing is spooled, the session is told plainly.
            println!(
                "{}",
                json!({"hookSpecificOutput": {"hookEventName": event, "additionalContext":
                    "Kelmo HOT unavailable (E_TRANSPORT). No selected pages were delivered: the Kelmo \
                     daemon on this machine did not answer."}})
            );
            return 0;
        }
        return match &options.state {
            Some(state) => match spool(state, &body) {
                Ok(_) => 0,
                Err(error) => {
                    eprintln!("E_STATE: the hook could not be spooled: {error}");
                    0
                }
            },
            None => {
                eprintln!("E_TRANSPORT: no Kelmo daemon answered and no --state names a spool");
                0
            }
        };
    };
    let offers = |op: &str| {
        hello["operations"]
            .as_array()
            .is_some_and(|ops| ops.iter().any(|item| item == op))
    };
    let op = if offers("edge.hook") {
        "edge.hook"
    } else {
        "state.hook"
    };
    let deadline = if context_event(&options.action).is_some() {
        CALL_DEADLINE
    } else {
        CAPTURE_DEADLINE
    };
    let header = json!({"v": 2, "kind": "call", "id": format!("{:032x}", 1u128 << 64 | std::process::id() as u128),
                        "op": op, "body_type": "json", "deadline_ms": deadline.as_millis() as u64});
    // A capture is on disk before it is sent: a hook the provider kills mid-call loses nothing.
    let inflight = if context_event(&options.action).is_none() {
        options
            .state
            .as_deref()
            .and_then(|state| match spool_inflight(state, &body) {
                Ok(paths) => Some(paths),
                Err(error) => {
                    eprintln!("E_STATE: the hook could not be spooled: {error}");
                    None
                }
            })
    } else {
        None
    };
    if !send(&mut stream, &header, &body).await {
        eprintln!("E_TRANSPORT: the daemon closed the connection");
        settle_capture(inflight, None);
        return 0;
    }
    let reply = tokio::time::timeout(
        deadline + Duration::from_secs(1),
        frames(&mut stream, &mut reader),
    )
    .await
    .ok()
    .flatten();
    let Some(event) = context_event(&options.action) else {
        settle_capture(inflight, reply);
        return 0;
    };
    match reply {
        Some((header, mut body)) if header["ok"] == true => {
            let ack = body
                .as_object_mut()
                .and_then(|object| object.remove("_kelmo_ack"));
            if body.as_object().is_some_and(|object| !object.is_empty()) {
                println!("{body}");
            }
            // The delivery is recorded only now that the session has the context.
            if let Some(token) = ack.filter(Value::is_string) {
                use std::io::Write;
                let _ = std::io::stdout().flush();
                let ack_op = if op == "edge.hook" {
                    "edge.ack"
                } else {
                    "state.ack"
                };
                let header = json!({"v": 2, "kind": "call", "id": format!("{:032x}", 2u128 << 64 | std::process::id() as u128),
                                    "op": ack_op, "body_type": "json", "deadline_ms": 2000});
                if send(&mut stream, &header, &json!({"token": token})).await {
                    let _ = tokio::time::timeout(
                        Duration::from_secs(2),
                        frames(&mut stream, &mut reader),
                    )
                    .await;
                }
            }
        }
        Some((header, _)) => {
            eprintln!("{}", json!({"error": header["error"]}));
            notice(
                event,
                header["error"]["code"].as_str().unwrap_or("E_PROCESSING"),
            );
        }
        None => {
            eprintln!("E_TRANSPORT: no reply from the daemon");
            notice(event, "E_TRANSPORT");
        }
    }
    0
}

/// After a capture call: the in-flight spool copy goes when the daemon took the event or refused it for good,
/// and becomes a visible spool entry when the outcome is unknown or worth retrying.
fn settle_capture(inflight: Option<(PathBuf, PathBuf)>, reply: Option<(Value, Value)>) {
    let keep = match &reply {
        Some((header, _)) if header["ok"] == true => false,
        Some((header, _)) => {
            eprintln!("{}", json!({"error": header["error"]}));
            header["error"]["retryable"] == true
        }
        None => {
            eprintln!("E_TRANSPORT: no reply from the daemon");
            true
        }
    };
    if let Some((hidden, visible)) = inflight {
        let result = if keep {
            std::fs::rename(&hidden, &visible)
        } else {
            std::fs::remove_file(&hidden)
        };
        if let Err(error) = result {
            eprintln!("E_STATE: the spooled copy could not be settled: {error}");
        }
    }
}

/// Tells the session that no pages were delivered this time, and why.
fn notice(event: &str, code: &str) {
    println!(
        "{}",
        json!({"hookSpecificOutput": {"hookEventName": event, "additionalContext": format!(
            "Kelmo HOT unavailable ({code}). No selected pages were delivered.")}})
    );
}
