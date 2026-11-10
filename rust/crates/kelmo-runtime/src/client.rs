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

/// Sends one call and returns (reply header, reply body).
pub async fn call(socket: &Path, request: Request<'_>) -> Result<(Value, Value), String> {
    let mut stream = UnixStream::connect(socket)
        .await
        .map_err(|e| format!("E_TRANSPORT: {}: {e}", socket.display()))?;
    let mut reader = kelmo_wire::FrameReader::new();
    let hello = json!({"v": 2, "kind": "hello", "id": random_id(), "body_type": "json",
                       "protocols": ["kelmo/2"], "operations": []});
    let bytes = kelmo_wire::encode(&hello, &Value::Null).map_err(|e| e.to_string())?;
    stream
        .write_all(&bytes)
        .await
        .map_err(|e| format!("E_TRANSPORT: {e}"))?;
    next_frame(&mut stream, &mut reader).await?;
    let mut header = json!({"v": 2, "kind": "call", "id": random_id(), "op": request.op,
                            "deadline_ms": request.deadline.as_millis() as u64, "body_type": "json"});
    if let Some(lane) = request.lane {
        header["lane"] = lane.into();
    }
    if let Some(key) = request.key {
        header["key"] = key.into();
    }
    let bytes = kelmo_wire::encode(&header, &request.body).map_err(|e| e.to_string())?;
    stream
        .write_all(&bytes)
        .await
        .map_err(|e| format!("E_TRANSPORT: {e}"))?;
    tokio::time::timeout(
        request.deadline + Duration::from_secs(5),
        next_frame(&mut stream, &mut reader),
    )
    .await
    .map_err(|_| "E_DEADLINE: no reply".to_owned())?
}
