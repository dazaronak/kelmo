//! A one-call client of a running daemon's protocol socket: hello, one call, its reply.

use std::path::Path;
use std::time::Duration;

use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;

pub struct Request<'a> {
    pub op: &'a str,
    pub body: Value,
    pub lane: Option<&'a str>,
    pub deadline: Duration,
    pub key: Option<&'a str>,
}

/// A call that failed: its message (`CODE: text`) and whether the caller may send it again. Nothing reached the
/// daemon before the call frame was written, so such a call is retryable; from that write on an unkeyed call
/// may have run and is not. A keyed call stays retryable, as the router answers for it: the key keeps an
/// operation that replays by key from running twice, while an operation the cores never replay ignores it.
#[derive(Debug)]
pub struct CallError {
    pub message: String,
    pub retryable: bool,
}

fn random_id() -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or_default();
    format!("{:016x}{:016x}", std::process::id() as u64, nanos as u64)
}

async fn next_frame(
    stream: &mut UnixStream,
    reader: &mut kelmo_wire::FrameReader,
) -> Result<(Value, Value), String> {
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let read = stream
            .read(&mut buffer)
            .await
            .map_err(|e| format!("E_TRANSPORT: {e}"))?;
        if read == 0 {
            return Err("E_TRANSPORT: the daemon closed the connection".into());
        }
        let mut frames = reader.feed(&buffer[..read]).map_err(|e| e.to_string())?;
        if !frames.is_empty() {
            return Ok(frames.remove(0));
        }
    }
}

/// Sends one call and returns (reply header, reply body). The call's deadline plus five seconds bounds all of
/// it, the connection, the hello and the write of the call frame included, so a daemon that accepts the
/// connection and then stalls ends the call with `E_DEADLINE`.
pub async fn call(socket: &Path, request: Request<'_>) -> Result<(Value, Value), CallError> {
    let end = tokio::time::Instant::now() + request.deadline + Duration::from_secs(5);
    let keyed = request.key.is_some();
    let unsent = |message: String| CallError {
        message,
        retryable: true,
    };
    let sent = |message: String| CallError {
        message,
        retryable: keyed,
    };
    let mut stream = tokio::time::timeout_at(end, UnixStream::connect(socket))
        .await
        .map_err(|_| unsent("E_DEADLINE: no connection".to_owned()))?
        .map_err(|e| unsent(format!("E_TRANSPORT: {}: {e}", socket.display())))?;
    let mut reader = kelmo_wire::FrameReader::new();
    let hello = json!({"v": 2, "kind": "hello", "id": random_id(), "body_type": "json",
                       "protocols": ["kelmo/2"], "operations": []});
    let bytes = kelmo_wire::encode(&hello, &Value::Null).map_err(|e| unsent(e.to_string()))?;
    tokio::time::timeout_at(end, stream.write_all(&bytes))
        .await
        .map_err(|_| unsent("E_DEADLINE: no hello".to_owned()))?
        .map_err(|e| unsent(format!("E_TRANSPORT: {e}")))?;
    tokio::time::timeout_at(end, next_frame(&mut stream, &mut reader))
        .await
        .map_err(|_| unsent("E_DEADLINE: no hello".to_owned()))?
        .map_err(unsent)?;
    let mut header = json!({"v": 2, "kind": "call", "id": random_id(), "op": request.op,
                            "deadline_ms": request.deadline.as_millis() as u64, "body_type": "json"});
    if let Some(lane) = request.lane {
        header["lane"] = lane.into();
    }
    if let Some(key) = request.key {
        header["key"] = key.into();
    }
    let bytes = kelmo_wire::encode(&header, &request.body).map_err(|e| unsent(e.to_string()))?;
    // From here the daemon may have the call: part of a frame may have reached it even if the write fails.
    tokio::time::timeout_at(end, stream.write_all(&bytes))
        .await
        .map_err(|_| sent("E_DEADLINE: the daemon did not take the call".to_owned()))?
        .map_err(|e| sent(format!("E_TRANSPORT: {e}")))?;
    tokio::time::timeout_at(end, next_frame(&mut stream, &mut reader))
        .await
        .map_err(|_| sent("E_DEADLINE: no reply".to_owned()))?
        .map_err(sent)
}
