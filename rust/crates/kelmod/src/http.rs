//! The HTTP MCP endpoint: rmcp's Streamable HTTP service on 127.0.0.1, served by hyper, in front of the
//! router. `list_tools` answers from the tool definitions the spec carries; `call_tool` becomes one
//! `state.tool` call (or the spec's `tool_op`) with `{tool, arguments}`, so the tool bodies stay in the core.
//!
//! Hardening, because hyper and rmcp leave each of these off or unbounded by default: a timer (without one
//! the header timeout does nothing), a header read timeout, header and buffer limits, a bound on open
//! connections, a timeout on each request, Host and Origin allow-lists that name the port, a request body
//! limit, a bound on live MCP sessions, and a bearer token that every request must carry.

use std::convert::Infallible;
use std::net::SocketAddr;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use http::{HeaderValue, Method, Request, Response, StatusCode};
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Full};
use hyper::body::Incoming;
use hyper::server::conn::http1;
use hyper_util::rt::{TokioIo, TokioTimer};
use kelmo_runtime::gateway::{CallError, Connection, Gateway};
use rmcp::model::{
    CallToolRequestParams, CallToolResponse, CallToolResult, ContentBlock, Implementation,
    ListToolsResult, PaginatedRequestParams, ServerCapabilities, ServerConfig, Tool,
};
use rmcp::service::RequestContext;
use rmcp::transport::streamable_http_server::session::local::LocalSessionManager;
use rmcp::transport::{StreamableHttpServerConfig, StreamableHttpService};
use rmcp::{ErrorData, RoleServer, ServerHandler};
use serde_json::{Value, json};
use tokio::net::TcpListener;
use tokio::sync::{Mutex, Semaphore};
use tokio_util::sync::CancellationToken;
use tower_service::Service as _;

type Body = BoxBody<Bytes, Infallible>;

/// Settings of the endpoint, from the spec's `http` object.
pub struct Settings {
    pub port: u16,
    pub token: String,
    pub tools: Vec<Tool>,
    pub tool_op: String,
    pub call_deadline: Duration,
    pub request_timeout: Duration,
    pub header_timeout: Duration,
    pub max_connections: usize,
    pub max_sessions: usize,
    pub max_body_bytes: usize,
}

fn number(value: &Value, name: &str, default: u64) -> Result<u64, String> {
    match value.get(name) {
        None | Some(Value::Null) => Ok(default),
        Some(item) => item
            .as_u64()
            .ok_or_else(|| format!("spec: http.{name} is not a non-negative integer")),
    }
}

/// Reads the bearer token: the file must hold it alone and be readable by its owner only.
fn read_token(path: &Path) -> Result<String, String> {
    use std::os::unix::fs::MetadataExt;
    let metadata = std::fs::metadata(path).map_err(|e| format!("{}: {e}", path.display()))?;
    if metadata.mode() & 0o077 != 0 {
        return Err(format!(
            "{}: the token file must not be readable by group or others",
            path.display()
        ));
    }
    if metadata.uid() != kelmo_runtime::sys::uid() {
        return Err(format!(
            "{}: the token file belongs to another user",
            path.display()
        ));
    }
    let token = std::fs::read_to_string(path)
        .map_err(|e| format!("{}: {e}", path.display()))?
        .trim()
        .to_owned();
    if token.len() < 32 || !token.bytes().all(|b| b.is_ascii_graphic()) {
        return Err(format!(
            "{}: the token must be at least 32 printable characters",
            path.display()
        ));
    }
    Ok(token)
}

impl Settings {
    pub fn from_spec(http: &Value) -> Result<Self, String> {
        let token_file = http
            .get("token_file")
            .and_then(Value::as_str)
            .ok_or("spec: http.token_file is missing")?;
        let token = read_token(Path::new(token_file))?;
        let tools = http
            .get("tools")
            .and_then(Value::as_array)
            .ok_or("spec: http.tools is not a list")?
            .iter()
            .map(|tool| {
                serde_json::from_value::<Tool>(tool.clone())
                    .map_err(|e| format!("spec: http.tools: {e}"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let port = number(http, "port", 0)?;
        Ok(Settings {
            port: u16::try_from(port).map_err(|_| "spec: http.port is not a port")?,
            token,
            tools,
            tool_op: http
                .get("tool_op")
                .and_then(Value::as_str)
                .unwrap_or("state.tool")
                .to_owned(),
            call_deadline: Duration::from_millis(number(http, "call_deadline_ms", 660_000)?),
            request_timeout: Duration::from_millis(number(http, "request_timeout_ms", 700_000)?),
            header_timeout: Duration::from_millis(number(http, "header_timeout_ms", 10_000)?),
            // An MCP session holds two connections (its requests and its event stream): 1280 serves the 512
            // sessions with room for reconnects.
            max_connections: number(http, "max_connections", 1280)?.max(1) as usize,
            max_sessions: number(http, "max_sessions", 512)?.max(1) as usize,
            max_body_bytes: number(http, "max_body_bytes", 4 * 1_048_576)?.max(1024) as usize,
        })
    }
}

/// Where the tool calls go: the router of this daemon, or a running daemon's socket (`mcp-stdio`).
pub enum Source {
    Gateway(Gateway),
    Socket(std::path::PathBuf),
}

/// The MCP server a session talks to. Each session has its own router connection, opened on first use and
/// opened again after it failed; it closes with the session, which cancels whatever the session left running.
#[derive(Clone)]
pub struct Tools {
    tools: Arc<Vec<Tool>>,
    op: Arc<str>,
    deadline: Duration,
    source: Arc<Source>,
    session: Option<Arc<str>>,
    connection: Arc<Mutex<Option<Arc<Connection>>>>,
}

static SESSIONS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);

impl Tools {
    pub fn new(tools: Vec<Tool>, op: &str, deadline: Duration, source: Source) -> Self {
        Tools {
            tools: Arc::new(tools),
            op: Arc::from(op),
            deadline,
            source: Arc::new(source),
            session: None,
            connection: Arc::new(Mutex::new(None)),
        }
    }

    /// The server of one new session: the same tools, its own connection and fairness key.
    fn for_session(&self) -> Self {
        let number = SESSIONS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        Tools {
            session: Some(Arc::from(format!("mcp-{number}"))),
            connection: Arc::new(Mutex::new(None)),
            ..self.clone()
        }
    }

    async fn connection(&self) -> Result<Arc<Connection>, CallError> {
        let mut slot = self.connection.lock().await;
        if let Some(connection) = slot.as_ref() {
            return Ok(connection.clone());
        }
        let connection = Arc::new(match self.source.as_ref() {
            Source::Gateway(gateway) => match &self.session {
                Some(session) => gateway.connect_session(session).await?,
                None => gateway.connect().await?,
            },
            Source::Socket(path) => Connection::connect_socket(path).await?,
        });
        *slot = Some(connection.clone());
        Ok(connection)
    }

    async fn forget(&self, failed: &Arc<Connection>) {
        let mut slot = self.connection.lock().await;
        if slot
            .as_ref()
            .is_some_and(|current| Arc::ptr_eq(current, failed))
        {
            *slot = None;
        }
    }
}

fn error_result(error: &CallError) -> CallToolResult {
    let mut result = CallToolResult::error(vec![ContentBlock::text(format!(
        "{}: {}",
        error.code, error.message
    ))]);
    let mut fields =
        json!({"code": error.code, "message": error.message, "retryable": error.retryable});
    if let Some(after) = error.retry_after_ms {
        fields["retry_after_ms"] = after.into();
    }
    result.structured_content = Some(json!({ "error": fields }));
    result
}

impl ServerHandler for Tools {
    fn get_info(&self) -> ServerConfig {
        ServerConfig::new(ServerCapabilities::builder().enable_tools().build())
            .with_server_info(Implementation::new("kelmo", env!("CARGO_PKG_VERSION")))
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items(self.tools.as_ref().clone()))
    }

    fn get_tool(&self, name: &str) -> Option<Tool> {
        self.tools.iter().find(|tool| tool.name == name).cloned()
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        if !self.tools.iter().any(|tool| tool.name == request.name) {
            return Err(ErrorData::invalid_params(
                format!("Unknown tool: {}", request.name),
                None,
            ));
        }
        let body = json!({"tool": request.name,
                          "arguments": Value::Object(request.arguments.unwrap_or_default())});
        let connection = match self.connection().await {
            Ok(connection) => connection,
            Err(error) => return Ok(error_result(&error).into()),
        };
        // A client that cancels the request, or drops it, cancels the call in the router.
        let outcome = tokio::select! {
            outcome = connection.call(&self.op, body, self.deadline) => outcome,
            _ = context.ct.cancelled() => Err(CallError {
                code: "E_CANCELLED".into(),
                message: "The client cancelled the request; its effect is unknown.".into(),
                retryable: false,
                retry_after_ms: None,
            }),
        };
        if matches!(&outcome, Err(error) if error.code == "E_TRANSPORT") {
            self.forget(&connection).await;
        }
        Ok(match outcome {
            Ok(value) => {
                let text = serde_json::to_string(&value).unwrap_or_default();
                let mut result = CallToolResult::success(vec![ContentBlock::text(text)]);
                // MCP structured content is an object; other values travel as text only.
                if value.is_object() {
                    result.structured_content = Some(value);
                }
                result
            }
            Err(error) => error_result(&error),
        }
        .into())
    }
}

fn plain(status: StatusCode, message: &str) -> Response<Body> {
    let mut response = Response::new(Full::new(Bytes::from(message.to_owned())).boxed());
    *response.status_mut() = status;
    response.headers_mut().insert(
        http::header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; charset=utf-8"),
    );
    response
}

/// Equal-length comparison whose time does not depend on where the first difference is.
fn same(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn authorized(request: &Request<Incoming>, token: &str) -> bool {
    request
        .headers()
        .get(http::header::AUTHORIZATION)
        .and_then(|value| value.as_bytes().strip_prefix(b"Bearer "))
        .is_some_and(|given| same(given, token.as_bytes()))
}

/// Writes `name` (mode 0600) into the runtime directory, by rename so a reader never sees half of it.
fn write_private(runtime: &Path, name: &str, text: &str) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let temporary = runtime.join(format!("{name}.tmp"));
    let _ = std::fs::remove_file(&temporary);
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    file.write_all(text.as_bytes())?;
    file.sync_all()?;
    std::fs::rename(&temporary, runtime.join(name))
}

/// `endpoint.json` (how to reach the endpoint) and `tools.json` (what `mcp-stdio` serves).
fn publish(runtime: &Path, port: u16, tools: &[Tool]) -> std::io::Result<()> {
    let tools = serde_json::to_string(tools).map_err(std::io::Error::other)?;
    write_private(runtime, "tools.json", &tools)?;
    let endpoint = json!({"url": format!("http://127.0.0.1:{port}/mcp"), "port": port,
                          "pid": std::process::id()});
    write_private(runtime, "endpoint.json", &endpoint.to_string())
}

/// `kelmod http-headers --state DIR (--root DIR | --runtime-dir DIR)`: the `Authorization` header a client's
/// header helper (Claude Code `headersHelper`, Codex `http_headers_helper`) sends, printed as one JSON object.
/// The token is given out only when this user's runtime directory names a live daemon on `port`, so a process
/// that took the port while the daemon was down never receives it, and no client file ever holds the token.
pub fn headers(state: &Path, runtime: &Path, port: u16) -> Result<String, String> {
    kelmo_runtime::ipc::trusted_directory(runtime)
        .map_err(|message| format!("E_RUNTIME: {message}"))?;
    let endpoint_path = runtime.join("endpoint.json");
    let endpoint: Value = std::fs::read_to_string(&endpoint_path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .ok_or_else(|| {
            format!(
                "E_RUNTIME: {}: no daemon serves the endpoint",
                endpoint_path.display()
            )
        })?;
    let pid = endpoint["pid"].as_u64().unwrap_or(0) as i32;
    if endpoint["port"].as_u64() != Some(u64::from(port))
        || pid <= 0
        || !kelmo_runtime::sys::alive(pid)
    {
        return Err(format!("E_RUNTIME: the daemon does not serve port {port}"));
    }
    let token =
        read_token(&state.join("http-token")).map_err(|message| format!("E_CONFIG: {message}"))?;
    Ok(json!({"Authorization": format!("Bearer {token}")}).to_string())
}

/// Reads the tool definitions a running daemon published, from a runtime directory of this user only.
pub fn published_tools(runtime: &Path) -> Result<Vec<Tool>, String> {
    kelmo_runtime::ipc::trusted_directory(runtime)
        .map_err(|message| format!("E_RUNTIME: {message}"))?;
    let path = runtime.join("tools.json");
    let text = std::fs::read_to_string(&path).map_err(|e| {
        format!(
            "E_RUNTIME: {}: {e}; the daemon serves no HTTP endpoint",
            path.display()
        )
    })?;
    serde_json::from_str(&text).map_err(|e| format!("E_RUNTIME: {}: {e}", path.display()))
}

/// Binds the endpoint's port on 127.0.0.1 before any core starts, so a taken port fails the daemon at once.
/// Runs inside the tokio runtime.
pub fn bind(settings: &Settings) -> std::io::Result<TcpListener> {
    let listener = std::net::TcpListener::bind(SocketAddr::from(([127, 0, 0, 1], settings.port)))?;
    listener.set_nonblocking(true)?;
    TcpListener::from_std(listener)
}

/// `kelmod mcp-stdio`: a stdio MCP server for hosts that cannot register an HTTP server. It serves the tools
/// the daemon published, sending each call over the daemon's socket.
pub async fn stdio(runtime: &Path) -> i32 {
    use rmcp::ServiceExt;
    let tools = match published_tools(runtime) {
        Ok(tools) => tools,
        Err(message) => {
            eprintln!("{message}");
            return 69;
        }
    };
    let server = Tools::new(
        tools,
        "state.tool",
        Duration::from_millis(660_000),
        Source::Socket(runtime.join("d.sock")),
    );
    match server
        .serve((tokio::io::stdin(), tokio::io::stdout()))
        .await
    {
        Ok(running) => {
            let _ = running.waiting().await;
            0
        }
        Err(error) => {
            eprintln!("E_PROTOCOL: {error}");
            1
        }
    }
}

/// Serves the endpoint until the daemon stops.
pub fn start(
    settings: Settings,
    listener: TcpListener,
    gateway: Gateway,
) -> tokio::task::JoinHandle<()> {
    let port = match listener.local_addr() {
        Ok(address) => address.port(),
        Err(error) => {
            eprintln!("kelmod: the HTTP endpoint has no address: {error}");
            return tokio::spawn(async {});
        }
    };
    if let Err(error) = publish(&gateway.runtime, port, &settings.tools) {
        eprintln!("kelmod: cannot publish the endpoint in the runtime directory: {error}");
    }
    let shutdown = CancellationToken::new();
    let config = StreamableHttpServerConfig::default()
        .with_allowed_hosts([format!("127.0.0.1:{port}"), format!("localhost:{port}")])
        .with_allowed_origins([
            format!("http://127.0.0.1:{port}"),
            format!("http://localhost:{port}"),
        ])
        .enforce_origin_validation()
        .with_max_request_body_bytes(settings.max_body_bytes)
        .with_cancellation_token(shutdown.clone());
    let sessions = Arc::new(LocalSessionManager::default());
    let tools = Tools::new(
        settings.tools,
        &settings.tool_op,
        settings.call_deadline,
        Source::Gateway(gateway.clone()),
    );
    let service =
        StreamableHttpService::new(move || Ok(tools.for_session()), sessions.clone(), config);
    // A new session is counted and created under one lock, so concurrent initialize requests cannot pass
    // the cap together.
    let opening = Arc::new(Mutex::new(()));
    let token: Arc<str> = Arc::from(settings.token.as_str());
    let permits = Arc::new(Semaphore::new(settings.max_connections));
    let (request_timeout, header_timeout, max_sessions) = (
        settings.request_timeout,
        settings.header_timeout,
        settings.max_sessions,
    );
    tokio::spawn(async move {
        loop {
            let permit = tokio::select! {
                permit = permits.clone().acquire_owned() => match permit {
                    Ok(permit) => permit,
                    Err(_) => break,
                },
                _ = gateway.stopping() => break,
            };
            let accepted = tokio::select! {
                accepted = listener.accept() => accepted,
                _ = gateway.stopping() => break,
            };
            let Ok((stream, _)) = accepted else {
                // Out of descriptors and the like: back off instead of spinning on the error.
                tokio::time::sleep(Duration::from_millis(50)).await;
                continue;
            };
            let (service, token, sessions) = (service.clone(), token.clone(), sessions.clone());
            let opening = opening.clone();
            let stopping = shutdown.clone();
            tokio::spawn(async move {
                let handler = hyper::service::service_fn(move |request: Request<Incoming>| {
                    let mut service = service.clone();
                    let token = token.clone();
                    let sessions = sessions.clone();
                    let opening = opening.clone();
                    async move {
                        if !authorized(&request, &token) {
                            // The connection closes after the refusal: a caller without the token must not keep
                            // one of the endpoint's connection slots.
                            let mut refused = plain(
                                StatusCode::UNAUTHORIZED,
                                "A valid bearer token is required.",
                            );
                            refused.headers_mut().insert(
                                http::header::CONNECTION,
                                HeaderValue::from_static("close"),
                            );
                            return Ok::<_, Infallible>(refused);
                        }
                        let new_session = request.method() == Method::POST
                            && !request.headers().contains_key("mcp-session-id");
                        let _opening = if new_session {
                            let guard = opening.lock_owned().await;
                            if sessions.sessions.read().await.len() >= max_sessions {
                                return Ok(plain(
                                    StatusCode::SERVICE_UNAVAILABLE,
                                    "Too many open MCP sessions; retry later.",
                                ));
                            }
                            Some(guard)
                        } else {
                            None
                        };
                        match tokio::time::timeout(request_timeout, service.call(request)).await {
                            Ok(Ok(response)) => Ok(response),
                            Ok(Err(never)) => match never {},
                            Err(_) => Ok(plain(
                                StatusCode::GATEWAY_TIMEOUT,
                                "The request took too long.",
                            )),
                        }
                    }
                });
                let connection = http1::Builder::new()
                    .timer(TokioTimer::new())
                    .header_read_timeout(header_timeout)
                    .max_headers(64)
                    .max_header_size(16 * 1024)
                    .max_buf_size(64 * 1024)
                    .serve_connection(TokioIo::new(stream), handler);
                tokio::pin!(connection);
                tokio::select! {
                    _ = connection.as_mut() => {}
                    _ = stopping.cancelled() => {
                        connection.as_mut().graceful_shutdown();
                        let _ = tokio::time::timeout(Duration::from_secs(3), connection).await;
                    }
                }
                drop(permit);
            });
        }
        shutdown.cancel();
        let _ = std::fs::remove_file(gateway.runtime.join("endpoint.json"));
        let _ = std::fs::remove_file(gateway.runtime.join("tools.json"));
    })
}
