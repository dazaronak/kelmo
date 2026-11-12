//! The router: one task that owns every mailbox, every call in flight and every channel's outbound queue.
//!
//! Callers (local connections and cores) send frames in; the router admits a call into its core's lane, or
//! refuses it at once (capability, overload, deadline), dispatches it when a slot is free under a fresh id,
//! and sends the reply back under the caller's id. Replies, cancels and heartbeats never wait in a mailbox.
//! Nothing here blocks: every send is to an unbounded channel whose depth is bounded by calls in flight.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde_json::{Map, Value, json};
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender};

use crate::lane::{Entry, Lane};
use crate::spec::{Replay, Spec};

pub type Outbound = UnboundedSender<Vec<u8>>;

/// When the router last finished handling an event. The daemon's tick reaches it every 50 ms, so a router that
/// is stuck or gone shows here as a growing age, and the daemon withholds its watchdog pings.
#[derive(Clone)]
pub struct Beat {
    epoch: Instant,
    last_ms: Arc<AtomicU64>,
}

impl Default for Beat {
    fn default() -> Self {
        Self::new()
    }
}

impl Beat {
    pub fn new() -> Self {
        Self {
            epoch: Instant::now(),
            last_ms: Arc::new(AtomicU64::new(0)),
        }
    }

    pub fn mark(&self) {
        let now = u64::try_from(self.epoch.elapsed().as_millis()).unwrap_or(u64::MAX);
        self.last_ms.store(now, Ordering::Relaxed);
    }

    pub fn age(&self) -> Duration {
        let last = Duration::from_millis(self.last_ms.load(Ordering::Relaxed));
        self.epoch.elapsed().saturating_sub(last)
    }
}

/// Who sent a call, and so where its reply goes.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Source {
    Client(u64),
    Core(usize),
}

pub enum Event {
    ClientOpen {
        conn: u64,
        out: Outbound,
    },
    ClientFrame {
        conn: u64,
        header: Value,
        body: Value,
    },
    ClientClosed {
        conn: u64,
    },
    CoreStarted {
        core: usize,
        out: Outbound,
        pid: u32,
    },
    CoreFrame {
        core: usize,
        header: Value,
        body: Value,
    },
    CoreExited {
        core: usize,
    },
    /// The core's supervisor ended for good (after a stop, or a spent restart budget): no process of it runs or
    /// will start again.
    CoreStopped {
        core: usize,
    },
    Memory {
        core: usize,
        bytes: Option<u64>,
    },
    Tick,
    Shutdown,
}

/// What the router asks a core's supervisor to do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Command {
    /// Kill now and restart (missed heartbeats, hard budget).
    Kill,
    /// The core was asked to stop; kill it if it is still there after the drain timeout, then restart.
    Restart,
    /// Stop for good (daemon shutdown). The router has sent `admin.stop`; the core drains with its channel open,
    /// so its own calls still get their replies, and is killed at its drain deadline.
    Stop,
    /// Stop for good when the router is lost: kelmod closes its half of the channel, which a core sees as a
    /// stop even without `admin.stop`, and kills it at its drain deadline.
    Close,
}

struct Call {
    source: Source,
    caller_id: String,
    core: usize,
    lane: String,
    op: String,
    origin: Value,
    key: Option<String>,
    body: Value,
    deadline: Instant,
    replay: Replay,
    /// The call came from a client connection (a session, attach, the HTTP gateway), not from a core.
    from_client: bool,
    /// The caller cancelled it while it ran: its reply is still forwarded, but it is never sent again.
    cancelled: bool,
    replayed: bool,
    dispatched: bool,
    abandoned: bool,
    /// Answered E_DEADLINE after deadline plus grace while the core still holds it.
    overdue: bool,
    admitted: Instant,
}

struct CoreState {
    name: String,
    lanes: BTreeMap<String, Lane>,
    out: Option<Outbound>,
    pid: Option<u32>,
    ready: bool,
    operations: Vec<String>,
    restarts: u64,
    pings: VecDeque<u64>,
    memory: Option<u64>,
    /// The soft budget in force: the spec's, or higher once pinned (see `memory`).
    soft: Option<u64>,
    /// The soft budget the spec set.
    soft_base: Option<u64>,
    hard: Option<u64>,
    over_soft_since: Option<Instant>,
    /// The last restart was a planned recycle. Cleared by a kill, and once the restarted core has been sampled
    /// more than `PIN_SAMPLES` times.
    planned: bool,
    /// Memory samples taken since the core last started.
    samples: u32,
    restarting: bool,
    /// An `admin.recycle` is out (its id): no new call is dispatched to the core until it answers, but
    /// heartbeats go on.
    recycle_pending: Option<u64>,
    /// When a pending recycle counts as declined for want of an answer.
    recycle_deadline: Option<Instant>,
    /// No new recycle is asked before this, after a decline.
    recycle_retry_at: Option<Instant>,
    /// The core declined the last recycle: the next decline of the streak is not logged again.
    recycle_declined: bool,
    /// The stop group: a core is stopped only once every core of a lower group has ended.
    group: usize,
    /// `admin.stop` and `Command::Stop` went out on the daemon's shutdown.
    stop_sent: bool,
    /// The supervisor ended for good.
    gone: bool,
    supervisor: UnboundedSender<Command>,
}

struct Client {
    out: Outbound,
    origin: Option<Value>,
}

/// Capability table: which ops a caller may call. Anything else is refused with E_FORBIDDEN.
fn allowed(caller: Option<&str>, op: &str) -> bool {
    let core = op.split('.').next().unwrap_or("");
    match caller {
        None => matches!(core, "state" | "edge" | "daemon"),
        Some("state") => matches!(core, "archive" | "embed"),
        Some("archive") => core == "embed",
        Some("edge") => core == "uplink",
        _ => false,
    }
}

/// The stop group of each named core: 0 for a core no other core of the list may call, else one more than the
/// highest group of its callers (`allowed`). So `state` and `edge` stop first, then `archive`, then `embed`,
/// and a core keeps its routes to every core it calls while it drains.
pub fn stop_groups(names: &[&str]) -> Vec<usize> {
    let mut groups = vec![0; names.len()];
    // A caller chain is at most as long as the list; the cap ends a cycle, which `allowed` does not have.
    for _ in 0..names.len() {
        for (caller, name) in names.iter().enumerate() {
            for (callee, other) in names.iter().enumerate() {
                if caller != callee && allowed(Some(*name), &format!("{other}.")) {
                    groups[callee] = groups[callee].max(groups[caller] + 1).min(names.len());
                }
            }
        }
    }
    groups
}

/// The drain a core of this stop group gets once its group is stopped.
pub fn group_drain(spec: &Spec, group: usize) -> Duration {
    if group == 0 {
        spec.drain_timeout
    } else {
        spec.drain_timeout.min(crate::spec::LATER_DRAIN)
    }
}

/// The stop groups of the spec's cores, in the spec's order.
pub fn spec_groups(spec: &Spec) -> Vec<usize> {
    let names: Vec<&str> = spec.cores.iter().map(|core| core.name.as_str()).collect();
    stop_groups(&names)
}

/// The longest the cores' stop takes after the client drain: every group's drain and slack, one after another.
pub fn stop_budget(spec: &Spec) -> Duration {
    let mut groups = spec_groups(spec);
    groups.sort_unstable();
    groups.dedup();
    // Each group may take its drain and the slack the router waits for its supervisors' reports.
    groups
        .into_iter()
        .map(|group| group_drain(spec, group) + GROUP_SLACK)
        .sum()
}

const GRACE: Duration = Duration::from_secs(2);
const RETRY_AFTER_MS: u64 = 100;
const STOPPING_RETRY_AFTER_MS: u64 = 1000;
const LATENCY_WINDOW: usize = 1000;
/// The longest a shutdown waits for admitted calls before it stops the cores. The cores then stop group by group
/// (`stop_budget`, at most 68 s), so the whole stop stays inside the service managers' 90 s stop timeout.
pub const ROUTER_DRAIN: Duration = Duration::from_secs(10);
/// How long a core has to answer `admin.recycle`; no answer is a decline.
const RECYCLE_DEADLINE: Duration = Duration::from_secs(10);
/// After a decline, the next recycle is asked no sooner than this.
const RECYCLE_RETRY: Duration = Duration::from_secs(30);
/// A core over soft within this many memory samples of a planned restart gets its soft budget pinned instead of
/// another recycle.
const PIN_SAMPLES: u32 = 3;
/// How long past its group's drain the router waits for a stopping core's supervisor to report its end before it
/// moves on to the next group anyway.
const GROUP_SLACK: Duration = Duration::from_secs(1);

pub struct Router {
    spec: Spec,
    cores: Vec<CoreState>,
    by_name: HashMap<String, usize>,
    clients: HashMap<u64, Client>,
    calls: HashMap<u64, Call>,
    by_caller: HashMap<(Source, String), u64>,
    pings: HashMap<u64, usize>,
    /// Ids of the `admin.recycle` calls out, and their cores: their reply bodies are read, unlike a ping's.
    recycles: HashMap<u64, usize>,
    latency: HashMap<String, VecDeque<u32>>,
    next: u64,
    last_heartbeat: Instant,
    started: Instant,
    shutting_down: bool,
    /// Set by a shutdown: admitted calls may finish until then, new client calls are refused as retryable.
    drain_until: Option<Instant>,
    /// The stop group being stopped, and when the router moves on without the end of its cores.
    stopping: Option<(usize, Instant)>,
    /// Every core has ended (or its group's time is up): the router's work is done.
    stopped: bool,
}

/// Whom a lane shares its slots between, round robin: a client, or one session of it when the origin names
/// one (each MCP session of the HTTP endpoint).
fn fairness_key(origin: &Value) -> String {
    let client = origin
        .get("client_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    match origin.get("session").and_then(Value::as_str) {
        Some(session) => format!("{client}\u{1f}{session}"),
        None => client.to_owned(),
    }
}

fn hex(id: u64) -> String {
    format!("{:016x}{:016x}", 0x6b656c6d6f64u64, id)
}

fn parse_hex(id: &str) -> Option<u64> {
    if id.len() != 32 {
        return None;
    }
    u64::from_str_radix(&id[16..], 16).ok()
}

fn error(code: &str, message: impl Into<String>, retryable: bool) -> Value {
    let mut map = Map::new();
    map.insert("code".into(), code.into());
    map.insert("message".into(), message.into().into());
    map.insert("retryable".into(), retryable.into());
    match code {
        _ if !retryable => {}
        "E_OVERLOADED" => {
            map.insert("retry_after_ms".into(), RETRY_AFTER_MS.into());
        }
        // A stopping daemon: the next one (service restart, upgrade) needs seconds, not a lane slot.
        "E_UNAVAILABLE" => {
            map.insert("retry_after_ms".into(), STOPPING_RETRY_AFTER_MS.into());
        }
        _ => {}
    }
    Value::Object(map)
}

fn send(out: &Outbound, header: &Value, body: &Value) {
    if let Ok(bytes) = kelmo_wire::encode(header, body) {
        let _ = out.send(bytes);
    }
}

impl Router {
    pub fn new(spec: Spec, supervisors: Vec<UnboundedSender<Command>>) -> Self {
        let mut cores = Vec::new();
        let mut by_name = HashMap::new();
        let groups = spec_groups(&spec);
        for (index, (core, supervisor)) in spec.cores.iter().zip(supervisors).enumerate() {
            by_name.insert(core.name.clone(), index);
            let soft = core
                .budget
                .as_ref()
                .map(|b| b.soft_bytes)
                .filter(|b| *b > 0);
            cores.push(CoreState {
                name: core.name.clone(),
                lanes: core
                    .lanes
                    .iter()
                    .map(|(name, lane)| (name.clone(), Lane::new(lane)))
                    .collect(),
                out: None,
                pid: None,
                ready: false,
                operations: Vec::new(),
                restarts: 0,
                pings: VecDeque::new(),
                memory: None,
                soft,
                soft_base: soft,
                hard: core
                    .budget
                    .as_ref()
                    .map(|b| b.hard_bytes)
                    .filter(|b| *b > 0),
                over_soft_since: None,
                planned: false,
                samples: 0,
                restarting: false,
                recycle_pending: None,
                recycle_deadline: None,
                recycle_retry_at: None,
                recycle_declined: false,
                group: groups[index],
                stop_sent: false,
                gone: false,
                supervisor,
            });
        }
        let now = Instant::now();
        Router {
            spec,
            cores,
            by_name,
            clients: HashMap::new(),
            calls: HashMap::new(),
            by_caller: HashMap::new(),
            pings: HashMap::new(),
            recycles: HashMap::new(),
            latency: HashMap::new(),
            next: 1,
            last_heartbeat: now,
            started: now,
            shutting_down: false,
            drain_until: None,
            stopping: None,
            stopped: false,
        }
    }

    pub fn all_ready(&self) -> bool {
        self.cores.iter().all(|core| core.ready)
    }

    pub async fn run(
        mut self,
        mut events: UnboundedReceiver<Event>,
        ready: UnboundedSender<()>,
        beat: Beat,
    ) -> Self {
        let mut announced = false;
        beat.mark();
        while let Some(event) = events.recv().await {
            self.handle(event);
            beat.mark();
            if !announced && self.all_ready() {
                announced = true;
                let _ = ready.send(());
            }
            if self.shutting_down {
                self.stop_when_drained();
            }
            if self.stopped {
                break;
            }
        }
        self
    }

    fn id(&mut self) -> u64 {
        let id = self.next;
        self.next += 1;
        id
    }

    pub fn handle(&mut self, event: Event) {
        match event {
            Event::ClientOpen { conn, out } => {
                self.clients.insert(conn, Client { out, origin: None });
            }
            Event::ClientFrame { conn, header, body } => self.client_frame(conn, header, body),
            Event::ClientClosed { conn } => self.client_closed(conn),
            Event::CoreStarted { core, out, pid } => {
                let state = &mut self.cores[core];
                state.out = Some(out);
                state.pid = Some(pid);
                state.ready = false;
                state.pings.clear();
                state.restarting = false;
                state.over_soft_since = None;
                state.samples = 0;
                state.recycle_pending = None;
                state.recycle_deadline = None;
                state.recycle_retry_at = None;
                state.recycle_declined = false;
                // A core that started while its stop went out (it was between restarts) stops too.
                if state.stop_sent {
                    self.send_stop(core);
                }
            }
            Event::CoreFrame { core, header, body } => self.core_frame(core, header, body),
            Event::CoreExited { core } => self.core_exited(core),
            Event::CoreStopped { core } => self.cores[core].gone = true,
            Event::Memory { core, bytes } => self.memory(core, bytes),
            Event::Tick => self.tick(),
            Event::Shutdown => self.shutdown(),
        }
    }

    // --- callers ---------------------------------------------------------------------------------------

    fn client_frame(&mut self, conn: u64, header: Value, body: Value) {
        let Some(client) = self.clients.get(&conn) else {
            return;
        };
        let kind = header["kind"].as_str().unwrap_or("");
        if client.origin.is_none() {
            if kind != "hello" {
                let out = client.out.clone();
                let id = header["id"].as_str().unwrap_or("").to_owned();
                self.refuse_to(&out, &id, "E_PROTOCOL", "Send hello first.", false);
                return;
            }
            let origin = header
                .get("origin")
                .cloned()
                .unwrap_or_else(|| self.spec.origin.clone());
            let out = client.out.clone();
            if let Some(client) = self.clients.get_mut(&conn) {
                client.origin = Some(origin);
            }
            let operations: Vec<Value> = self
                .spec
                .operations
                .keys()
                .filter(|op| allowed(None, op))
                .map(|op| Value::String(op.clone()))
                .collect();
            let reply = json!({"v": 2, "kind": "hello", "id": header["id"], "body_type": "json",
                               "protocols": ["kelmo/2"], "operations": operations});
            send(&out, &reply, &Value::Null);
            return;
        }
        match kind {
            "call" => {
                if header.get("origin").is_some() {
                    let out = client.out.clone();
                    let id = header["id"].as_str().unwrap_or("").to_owned();
                    self.refuse_to(
                        &out,
                        &id,
                        "E_PROTOCOL",
                        "A caller does not name its origin.",
                        false,
                    );
                    return;
                }
                if self.shutting_down {
                    let out = client.out.clone();
                    let id = header["id"].as_str().unwrap_or("").to_owned();
                    self.refuse_to(
                        &out,
                        &id,
                        "E_UNAVAILABLE",
                        "The daemon is stopping; retry once it is back.",
                        true,
                    );
                    return;
                }
                let origin = client.origin.clone().unwrap_or(Value::Null);
                self.admit(Source::Client(conn), header, body, origin);
            }
            "cancel" => {
                let id = header["id"].as_str().unwrap_or("").to_owned();
                self.cancel(Source::Client(conn), &id);
            }
            _ => {}
        }
    }

    fn client_closed(&mut self, conn: u64) {
        self.clients.remove(&conn);
        let ids: Vec<u64> = self
            .calls
            .iter()
            .filter(|(_, call)| call.source == Source::Client(conn))
            .map(|(id, _)| *id)
            .collect();
        for id in ids {
            // Nobody is left to read the reply, and nobody may see the call run again after a restart.
            if let Some(call) = self.calls.get_mut(&id)
                && call.dispatched
            {
                call.abandoned = true;
                let (source, caller_id) = (call.source, call.caller_id.clone());
                self.by_caller.remove(&(source, caller_id));
            }
            self.drop_or_cancel(id);
        }
    }

    fn refuse_to(
        &self,
        out: &Outbound,
        caller_id: &str,
        code: &str,
        message: &str,
        retryable: bool,
    ) {
        if kelmo_wire::header::validate_header(&json!({"v": 2, "kind": "cancel", "id": caller_id,
                                                         "body_type": "json"}))
        .is_err()
        {
            return;
        }
        let header = kelmo_wire::header::reply(caller_id, Some(error(code, message, retryable)));
        send(out, &header, &Value::Null);
    }

    fn out_of(&self, source: Source) -> Option<Outbound> {
        match source {
            Source::Client(conn) => self.clients.get(&conn).map(|c| c.out.clone()),
            Source::Core(core) => self.cores[core].out.clone(),
        }
    }

    fn refuse(&self, source: Source, caller_id: &str, code: &str, message: &str, retryable: bool) {
        if let Some(out) = self.out_of(source) {
            self.refuse_to(&out, caller_id, code, message, retryable);
        }
    }

    /// Admits a call into its lane or refuses it at once.
    fn admit(&mut self, source: Source, header: Value, body: Value, origin: Value) {
        let caller_id = header["id"].as_str().unwrap_or("").to_owned();
        let op = header["op"].as_str().unwrap_or("").to_owned();
        let caller_core = match source {
            Source::Core(core) => Some(self.cores[core].name.clone()),
            Source::Client(_) => None,
        };
        if !allowed(caller_core.as_deref(), &op) {
            self.refuse(
                source,
                &caller_id,
                "E_FORBIDDEN",
                "This caller may not call this operation.",
                false,
            );
            return;
        }
        if op == "daemon.status" {
            // Pids, memory and queue depths of every core are for the machine's own callers only.
            if origin.get("client_id") != self.spec.origin.get("client_id") {
                self.refuse(
                    source,
                    &caller_id,
                    "E_FORBIDDEN",
                    "Daemon status is served to local callers only.",
                    false,
                );
                return;
            }
            let status = self.status();
            if let Some(out) = self.out_of(source) {
                send(&out, &kelmo_wire::header::reply(&caller_id, None), &status);
            }
            return;
        }
        let Some(spec) = self.spec.operations.get(&op).cloned() else {
            self.refuse(
                source,
                &caller_id,
                "E_PROTOCOL",
                "Unknown operation.",
                false,
            );
            return;
        };
        let core_name = op.split('.').next().unwrap_or("");
        let Some(&core) = self.by_name.get(core_name) else {
            self.refuse(
                source,
                &caller_id,
                "E_PROTOCOL",
                "No core serves this operation.",
                false,
            );
            return;
        };
        let lane = header
            .get("lane")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| spec.lanes[0].clone());
        if !spec.lanes.contains(&lane) || !self.cores[core].lanes.contains_key(&lane) {
            self.refuse(
                source,
                &caller_id,
                "E_PROTOCOL",
                "The operation does not run on this lane.",
                false,
            );
            return;
        }
        if self.by_caller.contains_key(&(source, caller_id.clone())) {
            self.refuse(
                source,
                &caller_id,
                "E_PROTOCOL",
                "A call with this id is already open.",
                false,
            );
            return;
        }
        let deadline_ms = header["deadline_ms"].as_u64().unwrap_or(0);
        if deadline_ms == 0 {
            self.refuse(
                source,
                &caller_id,
                "E_DEADLINE",
                "The deadline passed before admission.",
                true,
            );
            return;
        }
        let id = self.id();
        let origin_key = fairness_key(&origin);
        let entry = Entry {
            id,
            origin: origin_key,
        };
        let lane_state = self.cores[core]
            .lanes
            .get_mut(&lane)
            .expect("lane checked above");
        if lane_state.push(entry).is_err() {
            self.refuse(
                source,
                &caller_id,
                "E_OVERLOADED",
                format!("Lane {core_name}.{lane} is full.").as_str(),
                true,
            );
            return;
        }
        let now = Instant::now();
        self.by_caller.insert((source, caller_id.clone()), id);
        self.calls.insert(
            id,
            Call {
                source,
                caller_id,
                core,
                lane,
                op,
                origin,
                key: header.get("key").and_then(Value::as_str).map(str::to_owned),
                body,
                deadline: now + Duration::from_millis(deadline_ms),
                replay: spec.replay,
                from_client: matches!(source, Source::Client(_)),
                cancelled: false,
                replayed: false,
                dispatched: false,
                abandoned: false,
                overdue: false,
                admitted: now,
            },
        );
        self.pump(core);
    }

    /// Dispatches queued calls of a ready core while its lanes have free slots.
    fn pump(&mut self, core: usize) {
        let state = &self.cores[core];
        if !state.ready || state.restarting || state.recycle_pending.is_some() {
            return;
        }
        let Some(out) = self.cores[core].out.clone() else {
            return;
        };
        let lanes: Vec<String> = self.cores[core].lanes.keys().cloned().collect();
        let now = Instant::now();
        for lane in lanes {
            while let Some(entry) = self.cores[core].lanes.get_mut(&lane).and_then(Lane::take) {
                let Some(call) = self.calls.get_mut(&entry.id) else {
                    if let Some(lane) = self.cores[core].lanes.get_mut(&lane) {
                        lane.finished();
                    }
                    continue;
                };
                call.dispatched = true;
                let remaining = call.deadline.saturating_duration_since(now).as_millis() as u64;
                let mut header = json!({"v": 2, "kind": "call", "id": hex(entry.id), "op": call.op,
                                        "lane": call.lane, "deadline_ms": remaining.max(1),
                                        "body_type": "json", "origin": call.origin});
                if call.origin.is_null() {
                    header.as_object_mut().map(|h| h.remove("origin"));
                }
                if let Some(key) = &call.key {
                    header["key"] = key.clone().into();
                }
                send(&out, &header, &call.body);
            }
        }
    }

    fn cancel(&mut self, source: Source, caller_id: &str) {
        if let Some(&id) = self.by_caller.get(&(source, caller_id.to_owned())) {
            self.drop_or_cancel(id);
        }
    }

    /// A queued call is removed and answered E_CANCELLED; one in flight gets a cancel sent to its core.
    fn drop_or_cancel(&mut self, id: u64) {
        let Some(call) = self.calls.get_mut(&id) else {
            return;
        };
        if call.dispatched {
            call.cancelled = true;
            if let Some(out) = self.cores[call.core].out.clone() {
                send(
                    &out,
                    &json!({"v": 2, "kind": "cancel", "id": hex(id), "body_type": "json"}),
                    &Value::Null,
                );
            }
            return;
        }
        let (core, lane) = (call.core, call.lane.clone());
        if let Some(lane) = self.cores[core].lanes.get_mut(&lane) {
            lane.remove(id);
        }
        if let Some(call) = self.finish(id) {
            self.refuse(
                call.source,
                &call.caller_id,
                "E_CANCELLED",
                "Cancelled before it started.",
                true,
            );
        }
    }

    /// Forgets a call and returns it.
    fn finish(&mut self, id: u64) -> Option<Call> {
        let call = self.calls.remove(&id)?;
        self.by_caller
            .remove(&(call.source, call.caller_id.clone()));
        Some(call)
    }

    // --- cores -----------------------------------------------------------------------------------------

    fn core_frame(&mut self, core: usize, header: Value, body: Value) {
        match header["kind"].as_str().unwrap_or("") {
            "hello" => {
                let operations: Vec<String> = header["operations"]
                    .as_array()
                    .map(|items| {
                        items
                            .iter()
                            .filter_map(|i| i.as_str().map(str::to_owned))
                            .collect()
                    })
                    .unwrap_or_default();
                let state = &mut self.cores[core];
                state.operations = operations;
                state.ready = true;
                if let Some(out) = state.out.clone() {
                    let reply = json!({"v": 2, "kind": "hello", "id": header["id"], "body_type": "json",
                                       "protocols": ["kelmo/2"], "operations": []});
                    send(&out, &reply, &Value::Null);
                }
                self.pump(core);
            }
            "reply" => self.core_reply(core, header, body),
            "call" => {
                let origin = header.get("origin").cloned().unwrap_or(Value::Null);
                self.admit(Source::Core(core), header, body, origin);
            }
            "cancel" => {
                let id = header["id"].as_str().unwrap_or("").to_owned();
                self.cancel(Source::Core(core), &id);
            }
            _ => {}
        }
    }

    fn core_reply(&mut self, core: usize, header: Value, body: Value) {
        let Some(id) = header["id"].as_str().and_then(parse_hex) else {
            return;
        };
        if let Some(asked) = self.recycles.remove(&id) {
            self.recycle_answer(asked, id, &header, &body);
            return;
        }
        if let Some(pinged) = self.pings.remove(&id) {
            self.cores[pinged].pings.retain(|p| *p != id);
            return;
        }
        let Some(call) = self.calls.get(&id) else {
            return;
        };
        if call.core != core || !call.dispatched {
            return;
        }
        let lane = call.lane.clone();
        if let Some(lane) = self.cores[core].lanes.get_mut(&lane) {
            lane.finished();
        }
        let call = self.finish(id).expect("present above");
        if !call.abandoned {
            let elapsed = call.admitted.elapsed().as_millis().min(u32::MAX as u128) as u32;
            let window = self.latency.entry(call.op.clone()).or_default();
            if window.len() == LATENCY_WINDOW {
                window.pop_front();
            }
            window.push_back(elapsed);
            if let Some(out) = self.out_of(call.source) {
                let mut reply = header.clone();
                reply["id"] = call.caller_id.clone().into();
                send(&out, &reply, &body);
            }
        }
        self.pump(core);
    }

    /// A core's process ended. Calls in flight that came from a client, still have a waiting caller and were
    /// not cancelled are sent again once when
    /// their replay class allows it; every other call in flight is answered E_CORE_RESTARTED. Queued calls
    /// stay queued for the restarted core.
    fn core_exited(&mut self, core: usize) {
        let state = &mut self.cores[core];
        state.out = None;
        state.pid = None;
        state.ready = false;
        state.memory = None;
        state.restarts += 1;
        state.recycle_pending = None;
        state.recycle_deadline = None;
        for id in state.pings.drain(..) {
            self.pings.remove(&id);
        }
        self.recycles.retain(|_, asked| *asked != core);
        let in_flight: Vec<u64> = self
            .calls
            .iter()
            .filter(|(_, call)| call.core == core && call.dispatched)
            .map(|(id, _)| *id)
            .collect();
        for lane in self.cores[core].lanes.values_mut() {
            lane.in_flight = 0;
        }
        for id in in_flight {
            let call = self.calls.get_mut(&id).expect("listed above");
            let eligible = call.from_client && !call.replayed && !call.abandoned && !call.cancelled;
            let again = eligible
                && (matches!(call.replay, Replay::Read)
                    || matches!(call.replay, Replay::Keyed) && call.key.is_some());
            if again {
                call.replayed = true;
                call.dispatched = false;
                let entry = Entry {
                    id,
                    origin: fairness_key(&call.origin),
                };
                let lane = call.lane.clone();
                if let Some(lane) = self.cores[core].lanes.get_mut(&lane) {
                    lane.push_front(entry);
                }
            } else if let Some(call) = self.finish(id)
                && !call.abandoned
            {
                // Retryable only for a read or a keyed call: the caller resends under its own request key,
                // which the ledger settles at most once (section 4.6). A keyless write may have taken effect,
                // so a resend could apply it twice. kelmod itself never sends it again.
                let retryable =
                    matches!(call.replay, Replay::Read) || call.key.is_some() && !call.cancelled;
                self.refuse(
                    call.source,
                    &call.caller_id,
                    "E_CORE_RESTARTED",
                    "The core restarted during the call; its effect is unknown.",
                    retryable,
                );
            }
        }
    }

    fn memory(&mut self, core: usize, bytes: Option<u64>) {
        let now = Instant::now();
        let state = &mut self.cores[core];
        state.memory = bytes;
        let (Some(bytes), true) = (bytes, state.ready && !state.restarting) else {
            return;
        };
        state.samples = state.samples.saturating_add(1);
        if state.samples > PIN_SAMPLES {
            state.planned = false;
        }
        if let Some(hard) = state.hard.filter(|hard| bytes > *hard) {
            eprintln!(
                "kelmod: core {} uses {} MiB, over its hard budget of {} MiB; killing and restarting it",
                state.name,
                bytes >> 20,
                hard >> 20
            );
            self.kill(core);
            return;
        }
        if let Some(soft) = state.soft.filter(|soft| bytes > *soft) {
            match state.over_soft_since {
                None => {
                    eprintln!(
                        "kelmod: core {} uses {} MiB, over its soft budget of {} MiB; asking it to trim",
                        state.name,
                        bytes >> 20,
                        soft >> 20
                    );
                    state.over_soft_since = Some(now);
                    if let Some(out) = state.out.clone() {
                        let id = self.next;
                        self.next += 1;
                        send(
                            &out,
                            &json!({"v": 2, "kind": "call", "id": hex(id), "op": "admin.trim",
                                           "lane": "admin", "deadline_ms": 30000, "body_type": "json"}),
                            &json!({}),
                        );
                        self.pings.insert(id, core);
                    }
                }
                Some(_) if state.planned => {
                    // Still over soft after a trim, right after a planned restart: restarting does not bring
                    // this core down, so another one would only start a restart loop. Its soft budget is
                    // pinned above what it uses, below its hard budget, until kelmod restarts; growth past the
                    // pin is trimmed and recycled as before.
                    let mut pinned = soft.max(bytes / 4 * 5);
                    if let Some(hard) = state.hard {
                        pinned = pinned.min(hard / 10 * 9);
                    }
                    if pinned > soft {
                        eprintln!(
                            "kelmod: core {} uses {} MiB right after a planned restart, over its soft budget of \
                             {} MiB; a restart does not bring it down, so its soft budget is pinned at {} MiB \
                             until kelmod restarts (spec {} MiB, hard {})",
                            state.name,
                            bytes >> 20,
                            soft >> 20,
                            pinned >> 20,
                            state.soft_base.unwrap_or(soft) >> 20,
                            state.hard.map_or_else(
                                || "none".to_owned(),
                                |hard| format!("{} MiB", hard >> 20)
                            ),
                        );
                        state.soft = Some(pinned);
                    }
                    state.over_soft_since = None;
                }
                Some(_) => {
                    // A soft budget never ends background work: the core is asked, and only a core that says
                    // it is quiescent is restarted. A stopping daemon asks nothing.
                    let waiting = state.recycle_pending.is_some()
                        || state.recycle_retry_at.is_some_and(|at| now < at)
                        || state.stop_sent
                        || self.shutting_down;
                    let idle = !self.calls.values().any(|c| c.core == core && c.dispatched);
                    if !waiting && idle {
                        self.ask_recycle(core, now);
                    }
                }
            }
        } else {
            state.over_soft_since = None;
            state.recycle_declined = false;
        }
    }

    /// Sends `admin.recycle` and holds new calls for the core until it answers.
    fn ask_recycle(&mut self, core: usize, now: Instant) {
        let Some(out) = self.cores[core].out.clone() else {
            return;
        };
        let id = self.id();
        send(
            &out,
            &json!({"v": 2, "kind": "call", "id": hex(id), "op": "admin.recycle", "lane": "admin",
                    "deadline_ms": RECYCLE_DEADLINE.as_millis() as u64, "body_type": "json"}),
            &json!({}),
        );
        self.recycles.insert(id, core);
        let state = &mut self.cores[core];
        state.recycle_pending = Some(id);
        state.recycle_deadline = Some(now + RECYCLE_DEADLINE);
    }

    /// The core answered `admin.recycle`. Only `{"recycle": true}` in a successful reply is consent; an error
    /// (an older core answers E_PROTOCOL) is a decline.
    fn recycle_answer(&mut self, core: usize, id: u64, header: &Value, body: &Value) {
        if self.cores[core].recycle_pending != Some(id) {
            return;
        }
        let agreed =
            header["ok"].as_bool() == Some(true) && body["recycle"].as_bool() == Some(true);
        if self.shutting_down {
            // The daemon's stop has overtaken the recycle: the core stops with its group instead.
            let state = &mut self.cores[core];
            state.recycle_pending = None;
            state.recycle_deadline = None;
            self.pump(core);
            return;
        }
        if agreed {
            let state = &mut self.cores[core];
            state.recycle_pending = None;
            state.recycle_deadline = None;
            state.recycle_declined = false;
            self.graceful_restart(core);
            return;
        }
        let reason = if header["ok"].as_bool() == Some(true) {
            let busy: Vec<&str> = body["busy"]
                .as_array()
                .map(|names| names.iter().filter_map(Value::as_str).collect())
                .unwrap_or_default();
            format!("busy: {}", busy.join(", "))
        } else {
            format!(
                "error: {}",
                header["error"]["code"].as_str().unwrap_or("unknown")
            )
        };
        self.recycle_declined(core, &reason);
    }

    /// A recycle the core refused, failed or left unanswered: the core is kept, its queued calls go out, and
    /// it is not asked again for `RECYCLE_RETRY`.
    fn recycle_declined(&mut self, core: usize, reason: &str) {
        let state = &mut self.cores[core];
        if let Some(id) = state.recycle_pending.take() {
            self.recycles.remove(&id);
        }
        state.recycle_deadline = None;
        state.recycle_retry_at = Some(Instant::now() + RECYCLE_RETRY);
        if !state.recycle_declined {
            eprintln!(
                "kelmod: core {} declined a recycle ({reason}); keeping it",
                state.name
            );
        }
        state.recycle_declined = true;
        self.pump(core);
    }

    fn graceful_restart(&mut self, core: usize) {
        let state = &mut self.cores[core];
        eprintln!(
            "kelmod: core {} is still over its soft budget ({} MiB) after a trim and agreed to a recycle; \
             stopping and restarting it",
            state.name,
            state.memory.unwrap_or(0) >> 20
        );
        state.restarting = true;
        state.planned = true;
        if let Some(out) = state.out.clone() {
            let id = self.next;
            self.next += 1;
            send(
                &out,
                &json!({"v": 2, "kind": "call", "id": hex(id), "op": "admin.stop", "lane": "admin",
                               "deadline_ms": 30000, "body_type": "json"}),
                &json!({}),
            );
            self.pings.insert(id, core);
        }
        let _ = self.cores[core].supervisor.send(Command::Restart);
    }

    /// A core whose every slot in some lane is held by overdue calls (answered E_DEADLINE to their callers,
    /// never answered by the core) serves nothing on that lane any more; it is restarted, which frees the
    /// slots. A call left by a closed client is not counted: it is still within its deadline.
    fn restart_stuck_lanes(&mut self) {
        for core in 0..self.cores.len() {
            let state = &self.cores[core];
            if !state.ready || state.restarting {
                continue;
            }
            let stuck = state.lanes.iter().find(|(name, lane)| {
                let abandoned = self
                    .calls
                    .values()
                    .filter(|call| {
                        call.core == core && call.dispatched && call.overdue && &call.lane == *name
                    })
                    .count();
                lane.limit > 0 && abandoned >= lane.limit
            });
            if let Some((lane, _)) = stuck {
                eprintln!(
                    "kelmod: every slot of lane {lane} of core {} is held by a call past its deadline; \
                     killing and restarting it",
                    state.name
                );
                self.kill(core);
            }
        }
    }

    /// Kills a core so its supervisor restarts it. A kill is never a planned restart.
    fn kill(&mut self, core: usize) {
        let state = &mut self.cores[core];
        state.restarting = true;
        state.planned = false;
        let _ = state.supervisor.send(Command::Kill);
    }

    fn tick(&mut self) {
        let now = Instant::now();
        // Deadlines: a queued call past its deadline never reaches the core; a call in flight past it by
        // the grace period is answered and abandoned, and its core is asked to stop working on it.
        let mut expired = Vec::new();
        let mut overdue = Vec::new();
        for (id, call) in &self.calls {
            if call.abandoned {
                continue;
            }
            if !call.dispatched && now >= call.deadline {
                expired.push(*id);
            } else if call.dispatched && now >= call.deadline + GRACE {
                overdue.push(*id);
            }
        }
        for id in expired {
            let (core, lane) = {
                let call = &self.calls[&id];
                (call.core, call.lane.clone())
            };
            if let Some(lane) = self.cores[core].lanes.get_mut(&lane) {
                lane.remove(id);
            }
            if let Some(call) = self.finish(id) {
                self.refuse(
                    call.source,
                    &call.caller_id,
                    "E_DEADLINE",
                    "The deadline passed in the queue.",
                    true,
                );
            }
        }
        for id in overdue {
            let call = self.calls.get_mut(&id).expect("listed above");
            call.abandoned = true;
            call.overdue = true;
            let (source, caller_id, core) = (call.source, call.caller_id.clone(), call.core);
            self.by_caller.remove(&(source, caller_id.clone()));
            let retryable = matches!(call.replay, Replay::Read);
            self.refuse(
                source,
                &caller_id,
                "E_DEADLINE",
                "The core did not answer before the deadline.",
                retryable,
            );
            if let Some(out) = self.cores[core].out.clone() {
                send(
                    &out,
                    &json!({"v": 2, "kind": "cancel", "id": hex(id), "body_type": "json"}),
                    &Value::Null,
                );
            }
        }
        self.restart_stuck_lanes();
        for core in 0..self.cores.len() {
            if self.cores[core]
                .recycle_deadline
                .is_some_and(|deadline| now >= deadline)
            {
                self.recycle_declined(core, "no answer before the deadline");
            }
        }
        // Heartbeats bypass the mailboxes; a core that misses too many is killed and restarted. A core asked to
        // recycle is still pinged; one draining for the daemon's stop is not, so its drain is never cut short.
        if now.duration_since(self.last_heartbeat) >= self.spec.heartbeat {
            self.last_heartbeat = now;
            for core in 0..self.cores.len() {
                let state = &self.cores[core];
                if !state.ready || state.restarting || state.stop_sent {
                    continue;
                }
                if state.pings.len() as u32 >= self.spec.heartbeat_misses {
                    eprintln!(
                        "kelmod: core {} left {} heartbeats unanswered; killing and restarting it",
                        state.name,
                        state.pings.len()
                    );
                    self.kill(core);
                    continue;
                }
                if let Some(out) = state.out.clone() {
                    let id = self.next;
                    self.next += 1;
                    send(
                        &out,
                        &json!({"v": 2, "kind": "call", "id": hex(id), "op": "admin.ping", "lane": "admin",
                                       "deadline_ms": self.spec.heartbeat.as_millis() as u64,
                                       "body_type": "json"}),
                        &json!({}),
                    );
                    self.pings.insert(id, core);
                    self.cores[core].pings.push_back(id);
                }
            }
        }
    }

    /// Starts the drain: new client calls are refused from now on, and the cores stop, group by group, once
    /// every admitted call (and every call a core makes on its behalf) has its reply, or when the drain time is
    /// up.
    fn shutdown(&mut self) {
        if self.shutting_down {
            return;
        }
        self.shutting_down = true;
        self.drain_until = Some(Instant::now() + ROUTER_DRAIN.min(self.spec.drain_timeout));
    }

    fn stop_when_drained(&mut self) {
        if self.stopped {
            return;
        }
        if self.stopping.is_some() {
            self.stop_next_group();
            return;
        }
        let expired = self.drain_until.is_none_or(|until| Instant::now() >= until);
        if !self.calls.is_empty() && !expired {
            return;
        }
        // The drain is over: every client still waiting gets an answer instead of a closed connection. Calls of
        // the cores keep their routes: a core drains with them once it is stopped.
        self.answer_unavailable(true);
        let first = self.cores.iter().map(|core| core.group).min().unwrap_or(0);
        self.stopping = Some((first, Instant::now()));
        self.stop_next_group();
    }

    /// Stops the cores group by group: the lowest group whose cores have not all ended gets `admin.stop` (once)
    /// and its supervisors a stop; a higher group waits until every core of it has ended or its drain plus a
    /// little slack is over. Meanwhile the router keeps routing the stopping cores' own calls to the cores still
    /// up, and their replies back. When every group is through, the router is done.
    fn stop_next_group(&mut self) {
        loop {
            let Some((group, since)) = self.stopping else {
                return;
            };
            let members: Vec<usize> = (0..self.cores.len())
                .filter(|core| self.cores[*core].group == group)
                .collect();
            let unsent: Vec<usize> = members
                .iter()
                .copied()
                .filter(|core| !self.cores[*core].stop_sent)
                .collect();
            for core in unsent {
                self.cores[core].stop_sent = true;
                self.send_stop(core);
                let _ = self.cores[core].supervisor.send(Command::Stop);
            }
            let ended = members.iter().all(|core| self.cores[*core].gone);
            let late = Instant::now() >= since + group_drain(&self.spec, group) + GROUP_SLACK;
            if !ended && !late {
                return;
            }
            let next = self
                .cores
                .iter()
                .map(|core| core.group)
                .filter(|g| *g > group)
                .min();
            match next {
                Some(next) => self.stopping = Some((next, Instant::now())),
                None => {
                    self.stopping = None;
                    self.stopped = true;
                    self.answer_unavailable(false);
                    return;
                }
            }
        }
    }

    /// Answers the open calls of clients only, or of every caller, E_UNAVAILABLE. A queued call never ran; one a
    /// core still holds may have taken effect, so only a read or a keyed call may be sent again.
    fn answer_unavailable(&mut self, clients_only: bool) {
        let ids: Vec<u64> = self
            .calls
            .iter()
            .filter(|(_, call)| !clients_only || call.from_client)
            .map(|(id, _)| *id)
            .collect();
        for id in ids {
            // A call a core still holds keeps its lane slot until the core answers it, so the cores that stay up
            // for a draining core's calls do not run out of slots; only its caller is answered now.
            if clients_only
                && let Some(call) = self.calls.get_mut(&id)
                && call.dispatched
            {
                if call.abandoned {
                    continue;
                }
                call.abandoned = true;
                let (source, caller_id) = (call.source, call.caller_id.clone());
                let retryable =
                    matches!(call.replay, Replay::Read) || call.key.is_some() && !call.cancelled;
                self.by_caller.remove(&(source, caller_id.clone()));
                self.refuse(
                    source,
                    &caller_id,
                    "E_UNAVAILABLE",
                    "The daemon stopped before the call finished.",
                    retryable,
                );
                continue;
            }
            if let Some(call) = self.finish(id)
                && !call.abandoned
            {
                let retryable = !call.dispatched
                    || matches!(call.replay, Replay::Read)
                    || call.key.is_some() && !call.cancelled;
                self.refuse(
                    call.source,
                    &call.caller_id,
                    "E_UNAVAILABLE",
                    "The daemon stopped before the call finished.",
                    retryable,
                );
            }
        }
    }

    /// Sends `admin.stop` to a running core.
    fn send_stop(&mut self, core: usize) {
        let Some(out) = self.cores[core].out.clone() else {
            return;
        };
        let id = self.id();
        send(
            &out,
            &json!({"v": 2, "kind": "call", "id": hex(id), "op": "admin.stop", "lane": "admin",
                    "deadline_ms": 30000, "body_type": "json"}),
            &json!({}),
        );
        self.pings.insert(id, core);
    }

    pub fn restarts(&self, core: usize) -> u64 {
        self.cores[core].restarts
    }

    fn status(&self) -> Value {
        let cores: Vec<Value> = self
            .cores
            .iter()
            .enumerate()
            .map(|(index, core)| {
                let lanes: Map<String, Value> = core
                    .lanes
                    .iter()
                    .map(|(name, lane)| {
                        (name.clone(), json!({"queued": lane.queued(), "in_flight": lane.in_flight,
                                              "limit": lane.limit, "queue": lane.capacity}))
                    })
                    .collect();
                json!({"name": core.name, "pid": core.pid, "ready": core.ready, "restarts": core.restarts,
                       "memory_bytes": core.memory, "lanes": lanes, "operations": core.operations,
                       "budget": {"soft_bytes": core.soft, "hard_bytes": core.hard,
                                  "spec_soft_bytes": core.soft_base, "pinned": core.soft != core.soft_base},
                       "index": index})
            })
            .collect();
        let p95: Map<String, Value> = self
            .latency
            .iter()
            .map(|(op, window)| {
                let mut sorted: Vec<u32> = window.iter().copied().collect();
                sorted.sort_unstable();
                let index = ((sorted.len() as f64) * 0.95) as usize;
                (op.clone(), json!(sorted[index.min(sorted.len() - 1)]))
            })
            .collect();
        json!({"pid": std::process::id(), "uptime_ms": self.started.elapsed().as_millis() as u64,
               "memory_bytes": crate::sys::own_resident_bytes(), "connections": self.clients.len(),
               "calls": self.calls.len(), "cores": cores, "p95_ms": p95,
               "meta": self.spec.meta})
    }
}

#[cfg(test)]
#[path = "router_tests.rs"]
mod tests;
