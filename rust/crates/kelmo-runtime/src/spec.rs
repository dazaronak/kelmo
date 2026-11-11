//! The daemon's specification: which cores to start and how, their lanes and budgets, and the operation table.
//!
//! It arrives as JSON. In production `kelmo core-spec --root R --json` writes it (Python reads `daemon.toml`
//! and the confinement rules); tests write it by hand.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::Value;

/// The longest drain a core gets on a stop.
pub const MAX_DRAIN: Duration = Duration::from_secs(25);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Order {
    /// One queue, first in first out.
    Fifo,
    /// One queue per origin, served in turn; each origin's queue is first in first out.
    RoundRobin,
}

#[derive(Debug, Clone)]
pub struct LaneSpec {
    pub limit: usize,
    pub queue: usize,
    pub order: Order,
}

#[derive(Debug, Clone)]
pub struct Budget {
    pub soft_bytes: u64,
    pub hard_bytes: u64,
}

#[derive(Debug, Clone)]
pub struct CoreSpec {
    pub name: String,
    pub argv: Vec<String>,
    pub env: BTreeMap<String, String>,
    pub lanes: BTreeMap<String, LaneSpec>,
    pub budget: Option<Budget>,
    pub start_timeout: Duration,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Replay {
    Read,
    Keyed,
    Never,
}

#[derive(Debug, Clone)]
pub struct OperationSpec {
    pub lanes: Vec<String>,
    pub replay: Replay,
}

#[derive(Debug, Clone)]
pub struct Spec {
    pub root: PathBuf,
    pub state_dir: PathBuf,
    pub runtime_dir: Option<PathBuf>,
    /// The identity a local connection speaks for unless its hello names a client.
    pub origin: Value,
    pub cores: Vec<CoreSpec>,
    pub operations: BTreeMap<String, OperationSpec>,
    pub heartbeat: Duration,
    pub heartbeat_misses: u32,
    pub sample: Duration,
    pub restart_max: usize,
    pub restart_window: Duration,
    pub drain_timeout: Duration,
    /// The HTTP MCP endpoint's settings, read by `kelmod`; absent when the daemon serves no endpoint.
    pub http: Option<Value>,
    /// What the spec was built for (role, server profile), echoed by `daemon.status` so a caller can tell
    /// whether the running daemon is the one it needs.
    pub meta: Value,
}

fn field<'a>(value: &'a Value, name: &str) -> Result<&'a Value, String> {
    value
        .get(name)
        .ok_or_else(|| format!("spec: '{name}' is missing"))
}

fn text(value: &Value, name: &str) -> Result<String, String> {
    field(value, name)?
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| format!("spec: '{name}' is not a string"))
}

fn number(value: &Value, name: &str, default: u64) -> Result<u64, String> {
    match value.get(name) {
        None => Ok(default),
        Some(item) => item
            .as_u64()
            .ok_or_else(|| format!("spec: '{name}' is not a non-negative integer")),
    }
}

fn millis(value: &Value, name: &str, default: u64) -> Result<Duration, String> {
    number(value, name, default).map(Duration::from_millis)
}

impl Spec {
    pub fn from_json(value: &Value) -> Result<Self, String> {
        let root = PathBuf::from(text(value, "root")?);
        let state_dir = match value.get("state_dir") {
            Some(_) => PathBuf::from(text(value, "state_dir")?),
            None => root.join(".state"),
        };
        let runtime_dir = value
            .get("runtime_dir")
            .and_then(Value::as_str)
            .map(PathBuf::from);
        let origin = field(value, "origin")?.clone();
        if origin.get("client_id").and_then(Value::as_str).is_none() {
            return Err("spec: origin.client_id is missing".into());
        }
        let mut cores = Vec::new();
        for core in field(value, "cores")?
            .as_array()
            .ok_or("spec: 'cores' is not a list")?
        {
            cores.push(core_from_json(core)?);
        }
        let mut operations = BTreeMap::new();
        for op in field(value, "operations")?
            .as_array()
            .ok_or("spec: 'operations' is not a list")?
        {
            let name = text(op, "name")?;
            let lanes = field(op, "lanes")?
                .as_array()
                .ok_or("spec: operation lanes is not a list")?
                .iter()
                .map(|lane| lane.as_str().map(str::to_owned))
                .collect::<Option<Vec<_>>>()
                .ok_or("spec: operation lane is not a string")?;
            if lanes.is_empty() {
                return Err(format!("spec: operation {name} has no lane"));
            }
            let replay = match text(op, "replay")?.as_str() {
                "read" => Replay::Read,
                "keyed" => Replay::Keyed,
                "never" => Replay::Never,
                other => return Err(format!("spec: unknown replay class {other}")),
            };
            operations.insert(name, OperationSpec { lanes, replay });
        }
        Ok(Spec {
            root,
            state_dir,
            runtime_dir,
            origin,
            cores,
            operations,
            heartbeat: millis(value, "heartbeat_ms", 5000)?,
            heartbeat_misses: number(value, "heartbeat_misses", 3)? as u32,
            sample: millis(value, "sample_ms", 10_000)?,
            restart_max: number(value, "restart_max", 5)? as usize,
            restart_window: millis(value, "restart_window_ms", 60_000)?,
            // A stop takes the router's wait (at most 13 s), then the cores' drain from their stop: with 25 s
            // that is about 38 s, inside the service managers' 40 s stop timeout; a longer drain would be cut by
            // their SIGKILL.
            drain_timeout: millis(value, "drain_timeout_ms", 20_000)?.min(MAX_DRAIN),
            http: value.get("http").filter(|http| !http.is_null()).cloned(),
            meta: value.get("meta").cloned().unwrap_or(Value::Null),
        })
    }
}

fn core_from_json(core: &Value) -> Result<CoreSpec, String> {
    let name = text(core, "name")?;
    let argv = field(core, "argv")?
        .as_array()
        .ok_or("spec: core argv is not a list")?
        .iter()
        .map(|arg| arg.as_str().map(str::to_owned))
        .collect::<Option<Vec<_>>>()
        .ok_or("spec: core argv holds a non-string")?;
    if argv.is_empty() {
        return Err(format!("spec: core {name} has an empty argv"));
    }
    let mut env = BTreeMap::new();
    if let Some(map) = core.get("env").and_then(Value::as_object) {
        for (key, item) in map {
            let item = item
                .as_str()
                .ok_or("spec: core env value is not a string")?;
            env.insert(key.clone(), item.to_owned());
        }
    }
    let mut lanes = BTreeMap::new();
    for (lane, entry) in field(core, "lanes")?
        .as_object()
        .ok_or("spec: core lanes is not an object")?
    {
        let order = match entry.get("order").and_then(Value::as_str).unwrap_or("fifo") {
            "fifo" => Order::Fifo,
            "rr" | "round_robin" => Order::RoundRobin,
            other => return Err(format!("spec: unknown lane order {other}")),
        };
        lanes.insert(
            lane.clone(),
            LaneSpec {
                limit: number(entry, "limit", 1)?.max(1) as usize,
                queue: number(entry, "queue", 256)? as usize,
                order,
            },
        );
    }
    let budget = match core.get("budget") {
        None | Some(Value::Null) => None,
        Some(budget) => Some(Budget {
            soft_bytes: number(budget, "soft_mb", 0)? * 1_048_576,
            hard_bytes: number(budget, "hard_mb", 0)? * 1_048_576,
        }),
    };
    Ok(CoreSpec {
        name,
        argv,
        env,
        lanes,
        budget,
        start_timeout: millis(core, "start_timeout_ms", 20_000)?,
    })
}
