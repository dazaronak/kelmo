//! In-process callers of the router: one gateway connection per long-lived front end (the HTTP MCP endpoint),
//! multiplexing many calls by id. A connection is admitted, lane-bound and cancelled exactly like a socket
//! client, so the router's rules apply to it unchanged.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{Value, json};
use tokio::sync::mpsc::{UnboundedSender, unbounded_channel};
use tokio::sync::{oneshot, watch};

use crate::router::Event;

/// How long the daemon may take to answer a connection's hello.
const HELLO_TIMEOUT: Duration = Duration::from_secs(5);

static CONNECTIONS: AtomicU64 = AtomicU64::new(1);
static CALLS: AtomicU64 = AtomicU64::new(1);

/// A connection number no other connection of this process has.
pub fn next_connection() -> u64 {
    CONNECTIONS.fetch_add(1, Ordering::Relaxed)
}

fn call_id() -> String {
    format!(
        "{:016x}{:016x}",
        std::process::id() as u64 | (1 << 63),
        CALLS.fetch_add(1, Ordering::Relaxed)
    )
}

/// What a front end needs from the daemon: a way to open router connections, and the shutdown signal.
#[derive(Clone)]
pub struct Gateway {
    events: UnboundedSender<Event>,
    shutdown: watch::Receiver<bool>,
    /// The daemon's own origin; a session connection speaks for it with its own `session` field.
    origin: Value,
    /// The daemon's runtime directory (mode 0700), where a front end publishes how to reach it.
    pub runtime: PathBuf,
}

impl Gateway {
    pub fn new(
        events: UnboundedSender<Event>,
        shutdown: watch::Receiver<bool>,
        origin: Value,
        runtime: PathBuf,
    ) -> Self {
        Gateway {
            events,
            shutdown,
            origin,
            runtime,
        }
    }

    /// Resolves once the daemon starts to stop.
    pub async fn stopping(&self) {
        let mut shutdown = self.shutdown.clone();
        let _ = shutdown.wait_for(|stopping| *stopping).await;
    }

    /// Opens a connection that speaks for the daemon's own origin.
    pub async fn connect(&self) -> Result<Connection, CallError> {
        Connection::open(self.events.clone(), None).await
    }

    /// Opens a connection for one session of the daemon's own origin. The router shares each lane round
    /// robin between sessions, so one busy session cannot hold every slot.
    pub async fn connect_session(&self, session: &str) -> Result<Connection, CallError> {
        let mut origin = self.origin.clone();
        if let Some(fields) = origin.as_object_mut() {
            fields.insert("session".into(), session.into());
        }
        Connection::open(self.events.clone(), Some(origin)).await
    }
}

/// A refused or failed call, with the router's error fields.
#[derive(Debug, Clone)]
pub struct CallError {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    /// The router's back-off hint for an overloaded lane.
    pub retry_after_ms: Option<u64>,
}

impl CallError {
    /// The call never left this process: sending it again is safe.
    fn transport(message: &str) -> Self {
        CallError {
            code: "E_TRANSPORT".into(),
            message: message.into(),
            retryable: true,
            retry_after_ms: None,
        }
    }

    /// The call was sent and its answer was lost: it may have taken effect.
    fn lost(message: &str) -> Self {
        CallError {
            retryable: false,
            ..CallError::transport(&format!("{message} The effect of the call is unknown."))
        }
    }
}

type Pending = Arc<Mutex<HashMap<String, oneshot::Sender<(Value, Value)>>>>;

/// Where a connection's frames go: straight into the router of this process, or over the daemon's socket.
enum Path {
    Router {
        conn: u64,
        events: UnboundedSender<Event>,
    },
    Socket(UnboundedSender<Vec<u8>>),
}

/// One router connection; calls on it run concurrently and are matched by id.
pub struct Connection {
    path: Path,
    pending: Pending,
}

/// Hands each reply to the call waiting for its id; at the end every waiter sees its sender closed.
fn dispatch(pending: &Pending, header: Value, body: Value) {
    let id = header["id"].as_str().unwrap_or_default().to_owned();
    let waiter = pending.lock().ok().and_then(|mut map| map.remove(&id));
    if let Some(waiter) = waiter {
        let _ = waiter.send((header, body));
    }
}

fn close_all(pending: &Pending) {
    if let Ok(mut map) = pending.lock() {
        map.clear();
    }
}

impl Connection {
    async fn open(
        events: UnboundedSender<Event>,
        origin: Option<Value>,
    ) -> Result<Self, CallError> {
        let conn = next_connection();
        let (out, mut queue) = unbounded_channel::<Vec<u8>>();
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        events
            .send(Event::ClientOpen { conn, out })
            .map_err(|_| CallError::transport("The daemon is stopping."))?;
        let reader = pending.clone();
        tokio::spawn(async move {
            while let Some(frame) = queue.recv().await {
                if let Ok((header, body)) = kelmo_wire::decode(&frame) {
                    dispatch(&reader, header, body);
                }
            }
            close_all(&reader);
        });
        Self::greet(
            Connection {
                path: Path::Router { conn, events },
                pending,
            },
            origin,
        )
        .await
    }

    /// A connection over the daemon's protocol socket, speaking for the daemon's own origin. The runtime
    /// directory that holds the socket must be this user's own and private, or nothing is sent.
    pub async fn connect_socket(socket: &std::path::Path) -> Result<Self, CallError> {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};
        if let Some(directory) = socket.parent() {
            crate::ipc::trusted_directory(directory).map_err(|message| CallError {
                code: "E_RUNTIME".into(),
                message,
                retryable: false,
                retry_after_ms: None,
            })?;
        }
        let stream = tokio::net::UnixStream::connect(socket)
            .await
            .map_err(|e| CallError::transport(&format!("{}: {e}", socket.display())))?;
        let (mut read, mut write) = stream.into_split();
        let (out, mut queue) = unbounded_channel::<Vec<u8>>();
        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        tokio::spawn(async move {
            while let Some(bytes) = queue.recv().await {
                if write.write_all(&bytes).await.is_err() {
                    break;
                }
            }
        });
        let reader = pending.clone();
        tokio::spawn(async move {
            let mut frames = kelmo_wire::FrameReader::new();
            let mut buffer = vec![0u8; 64 * 1024];
            loop {
                let count = match read.read(&mut buffer).await {
                    Ok(0) | Err(_) => break,
                    Ok(count) => count,
                };
                let Ok(decoded) = frames.feed(&buffer[..count]) else {
                    break;
                };
                for (header, body) in decoded {
                    dispatch(&reader, header, body);
                }
            }
            close_all(&reader);
        });
        Self::greet(
            Connection {
                path: Path::Socket(out),
                pending,
            },
            None,
        )
        .await
    }

    async fn greet(connection: Connection, origin: Option<Value>) -> Result<Self, CallError> {
        let hello_id = call_id();
        let receiver = connection.expect(&hello_id);
        let mut hello = json!({"v": 2, "kind": "hello", "id": hello_id, "body_type": "json",
                               "protocols": ["kelmo/2"], "operations": []});
        if let Some(origin) = origin {
            hello["origin"] = origin;
        }
        connection.frame(hello, Value::Null)?;
        match tokio::time::timeout(HELLO_TIMEOUT, receiver).await {
            Ok(Ok(_)) => Ok(connection),
            Ok(Err(_)) => Err(CallError::transport("The daemon closed the connection.")),
            Err(_) => Err(CallError::transport(
                "The daemon did not answer hello in time.",
            )),
        }
    }

    fn expect(&self, id: &str) -> oneshot::Receiver<(Value, Value)> {
        let (sender, receiver) = oneshot::channel();
        if let Ok(mut map) = self.pending.lock() {
            map.insert(id.to_owned(), sender);
        }
        receiver
    }

    fn frame(&self, header: Value, body: Value) -> Result<(), CallError> {
        let stopped = || CallError::transport("The daemon is stopping.");
        match &self.path {
            Path::Router { conn, events } => events
                .send(Event::ClientFrame {
                    conn: *conn,
                    header,
                    body,
                })
                .map_err(|_| stopped()),
            Path::Socket(out) => {
                let bytes = kelmo_wire::encode(&header, &body).map_err(|error| CallError {
                    code: error.code.into(),
                    message: error.message,
                    retryable: false,
                    retry_after_ms: None,
                })?;
                out.send(bytes).map_err(|_| stopped())
            }
        }
    }

    /// Sends a call and waits for its reply. Dropping the returned future cancels the call: the router drops
    /// it from its queue, or forwards the cancel to the core that runs it.
    pub async fn call(
        &self,
        op: &str,
        body: Value,
        deadline: Duration,
    ) -> Result<Value, CallError> {
        let id = call_id();
        let receiver = self.expect(&id);
        let header = json!({"v": 2, "kind": "call", "id": id, "op": op, "body_type": "json",
                            "deadline_ms": deadline.as_millis() as u64});
        if let Err(error) = self.frame(header, body) {
            // Never sent: forget the waiter, nothing to cancel.
            if let Ok(mut map) = self.pending.lock() {
                map.remove(&id);
            }
            return Err(error);
        }
        let mut guard = CancelOnDrop {
            connection: self,
            id: Some(id),
        };
        let reply = receiver.await;
        guard.id = None;
        let (header, body) =
            reply.map_err(|_| CallError::lost("The daemon closed the connection."))?;
        if header["ok"].as_bool() == Some(true) {
            return Ok(body);
        }
        let error = &header["error"];
        Err(CallError {
            code: error["code"].as_str().unwrap_or("E_PROCESSING").to_owned(),
            message: error["message"].as_str().unwrap_or_default().to_owned(),
            retryable: error["retryable"].as_bool().unwrap_or(false),
            retry_after_ms: error["retry_after_ms"].as_u64(),
        })
    }
}

struct CancelOnDrop<'a> {
    connection: &'a Connection,
    id: Option<String>,
}

impl Drop for CancelOnDrop<'_> {
    fn drop(&mut self) {
        if let Some(id) = self.id.take() {
            if let Ok(mut map) = self.connection.pending.lock() {
                map.remove(&id);
            }
            let _ = self.connection.frame(
                json!({"v": 2, "kind": "cancel", "id": id, "body_type": "json"}),
                Value::Null,
            );
        }
    }
}

impl Drop for Connection {
    fn drop(&mut self) {
        // A socket connection closes when its writer's channel drops with it.
        if let Path::Router { conn, events } = &self.path {
            let _ = events.send(Event::ClientClosed { conn: *conn });
        }
    }
}
