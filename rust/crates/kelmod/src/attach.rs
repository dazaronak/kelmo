//! `kelmod attach --client-id ID [--protocol 1|2]`: the ssh forced command of a remote client. It bridges
//! its stdin and stdout to the daemon's socket and fixes the origin from its own argument, which the ssh gate
//! chose; nothing the client sends can name another origin.
//!
//! `--protocol 1` (default) keeps the `kelmo-client/1` line framing toward the client, with its refusals:
//! each line is one `state.client_request` call, answered in order, and an unterminated, oversized or
//! malformed line ends the channel with the `/1` refusal and exit status 1, as `serve-client` did. A request
//! the daemon could not take (a bad idempotency key, a frame too large) is refused for that request only.
//! End of input is a half-close: the request in flight and the lines already read are still answered.
//!
//! `--protocol 2` forwards kelmo/2 frames, and only those a remote client may send: calls of
//! [`REMOTE_OPERATIONS`] and cancels of its own calls. The client's `hello` is answered here with those
//! operations and never reaches the router, so the origin stays the one set at connect. When the client
//! closes its input, the bridge waits for the replies of the calls still open before it closes.

use std::collections::{HashSet, VecDeque};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use kelmo_wire::{FrameReader, MAX_FRAME, line};
use serde_json::{Value, json};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use tokio::net::unix::{OwnedReadHalf, OwnedWriteHalf};
use tokio::sync::mpsc;

const PROTOCOL_V1: &str = "kelmo-client/1";
/// The longest a `/1` request may run in the daemon: a create that waits for its result included.
const V1_DEADLINE: Duration = Duration::from_secs(660);
/// How long the daemon may take to answer hello.
const HELLO_TIMEOUT: Duration = Duration::from_secs(5);
/// The operations a remote client may call over `/2`.
const REMOTE_OPERATIONS: &[&str] = &["state.client_request"];
/// The idempotency key the server accepts: 1 to 200 visible ASCII characters.
const MAX_KEY: usize = 200;

fn id(counter: &mut u64) -> String {
    *counter += 1;
    format!("{:016x}{:016x}", std::process::id() as u64, *counter)
}

/// Reads one frame, keeping any further frames decoded with it in `pending`. Cancel safe: a read that did
/// not complete consumed nothing.
async fn next_frame(
    read: &mut OwnedReadHalf,
    reader: &mut FrameReader,
    pending: &mut VecDeque<(Value, Value)>,
) -> Result<(Value, Value), String> {
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        if let Some(frame) = pending.pop_front() {
            return Ok(frame);
        }
        let count = read
            .read(&mut buffer)
            .await
            .map_err(|e| format!("E_TRANSPORT: {e}"))?;
        if count == 0 {
            return Err("E_TRANSPORT: the daemon closed the connection".into());
        }
        pending.extend(reader.feed(&buffer[..count]).map_err(|e| e.to_string())?);
    }
}

async fn send(write: &mut OwnedWriteHalf, header: &Value, body: &Value) -> Result<(), String> {
    let bytes = kelmo_wire::encode(header, body).map_err(|e| e.to_string())?;
    write
        .write_all(&bytes)
        .await
        .map_err(|e| format!("E_TRANSPORT: {e}"))
}

/// The daemon's half of a bridge after hello.
struct Daemon {
    read: OwnedReadHalf,
    write: OwnedWriteHalf,
    reader: FrameReader,
    pending: VecDeque<(Value, Value)>,
}

/// Connects and says hello for `client_id` within [`HELLO_TIMEOUT`].
async fn connect(socket: &Path, client_id: &str) -> Result<Daemon, String> {
    if let Some(directory) = socket.parent() {
        kelmo_runtime::ipc::trusted_directory(directory)
            .map_err(|message| format!("E_RUNTIME: {message}"))?;
    }
    let stream = UnixStream::connect(socket)
        .await
        .map_err(|e| format!("E_TRANSPORT: {}: {e}", socket.display()))?;
    let (mut read, mut write) = stream.into_split();
    let mut reader = FrameReader::new();
    let mut pending = VecDeque::new();
    let hello = json!({"v": 2, "kind": "hello", "id": format!("{:032x}", std::process::id()),
                       "body_type": "json", "protocols": ["kelmo/2"], "operations": [],
                       "origin": {"client_id": client_id, "machine_tag": null, "session": null}});
    send(&mut write, &hello, &Value::Null).await?;
    let header = tokio::time::timeout(
        HELLO_TIMEOUT,
        next_frame(&mut read, &mut reader, &mut pending),
    )
    .await
    .map_err(|_| "E_TRANSPORT: the daemon did not answer hello in time".to_owned())??
    .0;
    if header["kind"] != "hello" {
        return Err(format!(
            "E_PROTOCOL: the daemon answered hello with {header}"
        ));
    }
    Ok(Daemon {
        read,
        write,
        reader,
        pending,
    })
}

/// Runs the bridge until either side closes; returns the exit status.
pub async fn run(socket: &Path, client_id: &str, protocol: u8) -> i32 {
    match protocol {
        2 => v2(socket, client_id).await,
        _ => v1(socket, client_id).await,
    }
}

fn reply_refusal(id: &Value, code: &str, message: &str) -> Option<Vec<u8>> {
    let header = json!({"v": 2, "kind": "reply", "id": id, "ok": false, "body_type": "json",
                        "error": {"code": code, "message": message, "retryable": false}});
    kelmo_wire::encode(&header, &Value::Null).ok()
}

async fn v2(socket: &Path, client_id: &str) -> i32 {
    let daemon = match connect(socket, client_id).await {
        Ok(daemon) => daemon,
        Err(message) => {
            eprintln!("{message}");
            return 69;
        }
    };
    let Daemon {
        read,
        mut write,
        reader: mut daemon_reader,
        pending: early,
    } = daemon;
    // One writer to stdout, whole frames only: daemon replies and local answers never split each other.
    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Vec<u8>>();
    let writer = tokio::spawn(async move {
        let mut stdout = tokio::io::stdout();
        while let Some(bytes) = out_rx.recv().await {
            if stdout.write_all(&bytes).await.is_err() || stdout.flush().await.is_err() {
                break;
            }
        }
    });
    // Ids of the calls forwarded and not yet answered; the downstream task removes each one on its reply.
    let open: Arc<Mutex<HashSet<String>>> = Arc::new(Mutex::new(HashSet::new()));
    let answered = Arc::new(tokio::sync::Notify::new());
    let to_client = out_tx.clone();
    let (open_down, answered_down) = (open.clone(), answered.clone());
    let mut downstream = tokio::spawn(async move {
        let forward = |header: Value, body: Value| -> bool {
            if header["kind"] == "reply"
                && let Some(id) = header["id"].as_str()
                && let Ok(mut open) = open_down.lock()
            {
                open.remove(id);
            }
            answered_down.notify_waiters();
            match kelmo_wire::encode(&header, &body) {
                Ok(bytes) => to_client.send(bytes).is_ok(),
                Err(_) => true,
            }
        };
        for (header, body) in early {
            if !forward(header, body) {
                return;
            }
        }
        let mut read = read;
        let mut buffer = vec![0u8; 64 * 1024];
        loop {
            let count = match read.read(&mut buffer).await {
                Ok(0) | Err(_) => break,
                Ok(count) => count,
            };
            let Ok(frames) = daemon_reader.feed(&buffer[..count]) else {
                break;
            };
            for (header, body) in frames {
                if !forward(header, body) {
                    return;
                }
            }
        }
        answered_down.notify_waiters();
    });
    let mut stdin = tokio::io::stdin();
    let mut reader = FrameReader::new();
    let mut buffer = vec![0u8; 64 * 1024];
    let mut daemon_gone = false;
    let status = 'bridge: loop {
        let count = tokio::select! {
            count = stdin.read(&mut buffer) => match count {
                Ok(0) | Err(_) => break 'bridge 0,
                Ok(count) => count,
            },
            _ = &mut downstream => { daemon_gone = true; break 'bridge 0 }
        };
        let frames = match reader.feed(&buffer[..count]) {
            Ok(frames) => frames,
            Err(error) => {
                eprintln!("{error}");
                break 1;
            }
        };
        for (header, body) in frames {
            match header["kind"].as_str().unwrap_or("") {
                "hello" => {
                    let reply = json!({"v": 2, "kind": "hello", "id": header["id"], "body_type": "json",
                                       "protocols": ["kelmo/2"], "operations": REMOTE_OPERATIONS});
                    if let Ok(bytes) = kelmo_wire::encode(&reply, &Value::Null) {
                        let _ = out_tx.send(bytes);
                    }
                }
                "call" => {
                    let op = header["op"].as_str().unwrap_or("");
                    if !REMOTE_OPERATIONS.contains(&op) {
                        if let Some(bytes) = reply_refusal(
                            &header["id"],
                            "E_FORBIDDEN",
                            "A remote client may not call this operation.",
                        ) {
                            let _ = out_tx.send(bytes);
                        }
                        continue;
                    }
                    if let (Some(id), Ok(mut open)) = (header["id"].as_str(), open.lock()) {
                        open.insert(id.to_owned());
                    }
                    if send(&mut write, &header, &body).await.is_err() {
                        break 'bridge 0;
                    }
                }
                "cancel" => {
                    let known = header["id"]
                        .as_str()
                        .is_some_and(|id| open.lock().is_ok_and(|open| open.contains(id)));
                    if known && send(&mut write, &header, &body).await.is_err() {
                        break 'bridge 0;
                    }
                }
                _ => {}
            }
        }
    };
    // The client closed its input: its open calls still get their replies, within their deadline.
    if !daemon_gone {
        let drained = async {
            loop {
                let waiting = answered.notified();
                if open.lock().map_or(true, |open| open.is_empty()) || downstream.is_finished() {
                    return;
                }
                waiting.await;
            }
        };
        let _ = tokio::time::timeout(V1_DEADLINE + Duration::from_secs(5), drained).await;
    }
    let _ = write.shutdown().await;
    drop(write);
    if !daemon_gone {
        let _ = tokio::time::timeout(Duration::from_secs(5), &mut downstream).await;
    }
    drop(out_tx);
    let _ = tokio::time::timeout(Duration::from_secs(5), writer).await;
    status
}

fn v1_refusal(request_id: Value, code: &str, message: &str, retryable: bool) -> Value {
    json!({"protocol": PROTOCOL_V1, "request_id": request_id, "ok": false,
           "error": {"code": code, "message": message, "retryable": retryable}})
}

async fn write_line(stdout: &mut tokio::io::Stdout, value: &Value) -> bool {
    let bytes = match line::encode_line(value) {
        Ok(bytes) => bytes,
        Err(error) => match line::encode_line(&v1_refusal(
            value.get("request_id").cloned().unwrap_or(Value::Null),
            error.code,
            &error.message,
            false,
        )) {
            Ok(bytes) => bytes,
            Err(_) => return false,
        },
    };
    stdout.write_all(&bytes).await.is_ok() && stdout.flush().await.is_ok()
}

/// Lines from the client, read on their own task so a call in flight still notices the client leaving.
/// Each item is one raw line (with its newline when it had one); the channel closes at end of input, after
/// a line without a newline (oversized or cut off) or on a read error.
fn client_lines() -> mpsc::Receiver<Vec<u8>> {
    let (sender, receiver) = mpsc::channel(4);
    tokio::spawn(async move {
        let mut stdin = BufReader::with_capacity(1 << 20, tokio::io::stdin());
        loop {
            let mut raw = Vec::new();
            // At most one byte past the bound is read, as `readline(MAX_FRAME + 1)` does.
            let mut limited = (&mut stdin).take(MAX_FRAME as u64 + 1);
            match limited.read_until(b'\n', &mut raw).await {
                Ok(0) | Err(_) => return,
                Ok(_) => {}
            }
            let complete = raw.last() == Some(&b'\n');
            if sender.send(raw).await.is_err() || !complete {
                return;
            }
        }
    });
    receiver
}

fn valid_key(key: &str) -> bool {
    (1..=MAX_KEY).contains(&key.len()) && key.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
}

/// What became of one `/1` request.
enum Outcome {
    Answer(Value),
    /// The channel ends after this line (or with no line when the client already left).
    End(Option<Value>, i32),
}

async fn v1_request(
    daemon: &mut Daemon,
    lines: &mut mpsc::Receiver<Vec<u8>>,
    queued: &mut VecDeque<Vec<u8>>,
    client_gone: &mut bool,
    counter: &mut u64,
    request: Value,
) -> Outcome {
    let request_id = request.get("request_id").cloned().unwrap_or(Value::Null);
    let key = match request.get("idempotency_key") {
        None | Some(Value::Null) => None,
        Some(Value::String(key)) if valid_key(key) => Some(key.clone()),
        Some(_) => {
            return Outcome::Answer(v1_refusal(
                request_id,
                "E_PROTOCOL",
                "The idempotency key must be 1 to 200 visible ASCII characters.",
                false,
            ));
        }
    };
    let call_id = id(counter);
    let mut header = json!({"v": 2, "kind": "call", "id": call_id, "op": "state.client_request",
                            "body_type": "json", "deadline_ms": V1_DEADLINE.as_millis() as u64});
    if let Some(key) = key {
        header["key"] = key.into();
    }
    let bytes = match kelmo_wire::encode(&header, &json!({ "request": request })) {
        Ok(bytes) => bytes,
        Err(error) => {
            return Outcome::Answer(v1_refusal(request_id, "E_LIMIT", &error.message, false));
        }
    };
    if daemon.write.write_all(&bytes).await.is_err() {
        let refusal = v1_refusal(
            request_id,
            "E_TRANSPORT",
            "The daemon closed the channel.",
            true,
        );
        return Outcome::End(Some(refusal), 1);
    }
    loop {
        tokio::select! {
            frame = next_frame(&mut daemon.read, &mut daemon.reader, &mut daemon.pending) => match frame {
                Ok((header, _)) if header["id"] != call_id => continue,
                Ok((header, body)) if header["ok"] == true => return Outcome::Answer(body),
                Ok((header, _)) => {
                    let error = &header["error"];
                    return Outcome::Answer(v1_refusal(
                        request_id,
                        error["code"].as_str().unwrap_or("E_PROCESSING"),
                        error["message"].as_str().unwrap_or_default(),
                        error["retryable"].as_bool().unwrap_or(false),
                    ));
                }
                Err(_) => {
                    // Sent and unanswered: whether it took effect is unknown.
                    let refusal = v1_refusal(
                        request_id,
                        "E_TRANSPORT",
                        "The daemon closed the channel; the effect of the request is unknown.",
                        false,
                    );
                    return Outcome::End(Some(refusal), 1);
                }
            },
            next = lines.recv(), if !*client_gone => match next {
                Some(raw) => queued.push_back(raw),
                // End of input is a half-close, as in `/2`: the requests already sent are still answered.
                None => *client_gone = true,
            },
        }
    }
}

async fn v1(socket: &Path, client_id: &str) -> i32 {
    let mut stdout = tokio::io::stdout();
    let mut daemon = match connect(socket, client_id).await {
        Ok(daemon) => daemon,
        Err(message) => {
            let code = message
                .split_once(':')
                .map_or("E_TRANSPORT", |(code, _)| code);
            write_line(
                &mut stdout,
                &v1_refusal(Value::Null, code, "The daemon is not reachable.", true),
            )
            .await;
            return 1;
        }
    };
    let mut lines = client_lines();
    let mut queued = VecDeque::new();
    let mut client_gone = false;
    let mut counter = 0u64;
    loop {
        let raw = match queued.pop_front() {
            Some(raw) => raw,
            None if client_gone => return 0,
            // `lines` stays open until end of input; after it, only `queued` is left.
            None => match lines.recv().await {
                Some(raw) => raw,
                None => return 0,
            },
        };
        let request = match line::decode_line(&raw) {
            Ok(request) => request,
            Err(error) => {
                write_line(
                    &mut stdout,
                    &v1_refusal(Value::Null, error.code, &error.message, false),
                )
                .await;
                return 1;
            }
        };
        let outcome = v1_request(
            &mut daemon,
            &mut lines,
            &mut queued,
            &mut client_gone,
            &mut counter,
            request,
        )
        .await;
        match outcome {
            Outcome::Answer(answer) => {
                if !write_line(&mut stdout, &answer).await {
                    return 1;
                }
            }
            Outcome::End(line, status) => {
                if let Some(line) = line {
                    write_line(&mut stdout, &line).await;
                }
                return status;
            }
        }
    }
}
