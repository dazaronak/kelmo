//! The router: one task that owns every mailbox, every call in flight and every channel's outbound queue.
//!
//! Callers (local connections and cores) send frames in; the router admits a call into its core's lane, or
//! refuses it at once (capability, overload, deadline), dispatches it when a slot is free under a fresh id,
//! and sends the reply back under the caller's id. Replies, cancels and heartbeats never wait in a mailbox.
//! Nothing here blocks: every send is to an unbounded channel whose depth is bounded by calls in flight.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::time::{Duration, Instant};

use serde_json::{Map, Value, json};
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender};

use crate::lane::{Entry, Lane};
use crate::spec::{Replay, Spec};

pub type Outbound = UnboundedSender<Vec<u8>>;

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
    /// Stop for good (daemon shutdown).
    Stop,
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
    via_gateway: bool,
    replayed: bool,
    dispatched: bool,
    abandoned: bool,
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
    soft: Option<u64>,
    hard: Option<u64>,
    over_soft_since: Option<Instant>,
    restarting: bool,
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

const GRACE: Duration = Duration::from_secs(2);
const RETRY_AFTER_MS: u64 = 100;
const LATENCY_WINDOW: usize = 1000;

pub struct Router {
    spec: Spec,
    cores: Vec<CoreState>,
    by_name: HashMap<String, usize>,
    clients: HashMap<u64, Client>,
    calls: HashMap<u64, Call>,
    by_caller: HashMap<(Source, String), u64>,
    pings: HashMap<u64, usize>,
    latency: HashMap<String, VecDeque<u32>>,
    next: u64,
    last_heartbeat: Instant,
    started: Instant,
    pub fatal: Option<i32>,
    shutting_down: bool,
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
    if code == "E_OVERLOADED" {
        map.insert("retry_after_ms".into(), RETRY_AFTER_MS.into());
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
        for (index, (core, supervisor)) in spec.cores.iter().zip(supervisors).enumerate() {
            by_name.insert(core.name.clone(), index);
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
                soft: core
                    .budget
                    .as_ref()
                    .map(|b| b.soft_bytes)
                    .filter(|b| *b > 0),
                hard: core
                    .budget
                    .as_ref()
                    .map(|b| b.hard_bytes)
                    .filter(|b| *b > 0),
                over_soft_since: None,
                restarting: false,
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
            latency: HashMap::new(),
            next: 1,
            last_heartbeat: now,
            started: now,
            fatal: None,
            shutting_down: false,
        }
    }

    pub fn all_ready(&self) -> bool {
        self.cores.iter().all(|core| core.ready)
    }

    pub async fn run(
        mut self,
        mut events: UnboundedReceiver<Event>,
        ready: UnboundedSender<()>,
    ) -> Self {
        let mut announced = false;
        while let Some(event) = events.recv().await {
            let stop = matches!(event, Event::Shutdown);
            self.handle(event);
            if !announced && self.all_ready() {
                announced = true;
                let _ = ready.send(());
            }
            if stop || self.fatal.is_some() {
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
            }
            Event::CoreFrame { core, header, body } => self.core_frame(core, header, body),
            Event::CoreExited { core } => self.core_exited(core),
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
        let origin_key = origin
            .get("client_id")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned();
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
                via_gateway: matches!(source, Source::Client(_)),
                replayed: false,
                dispatched: false,
                abandoned: false,
                admitted: now,
            },
        );
        self.pump(core);
    }

    /// Dispatches queued calls of a ready core while its lanes have free slots.
    fn pump(&mut self, core: usize) {
        if !self.cores[core].ready || self.cores[core].restarting {
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
        let Some(call) = self.calls.get(&id) else {
            return;
        };
        if call.dispatched {
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

    /// A core's process ended. Calls in flight that came through the gateway are sent again once when
    /// their replay class allows it; every other call in flight is answered E_CORE_RESTARTED. Queued calls
    /// stay queued for the restarted core.
    fn core_exited(&mut self, core: usize) {
        let state = &mut self.cores[core];
        state.out = None;
        state.pid = None;
        state.ready = false;
        state.memory = None;
        state.restarts += 1;
        for id in state.pings.drain(..) {
            self.pings.remove(&id);
        }
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
            let again = call.via_gateway
                && !call.replayed
                && !call.abandoned
                && matches!(call.replay, Replay::Read)
                || call.via_gateway
                    && !call.replayed
                    && !call.abandoned
                    && matches!(call.replay, Replay::Keyed)
                    && call.key.is_some();
            if again {
                call.replayed = true;
                call.dispatched = false;
                let entry = Entry {
                    id,
                    origin: call
                        .origin
                        .get("client_id")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                };
                let lane = call.lane.clone();
                if let Some(lane) = self.cores[core].lanes.get_mut(&lane) {
                    lane.push_front(entry);
                }
            } else if let Some(call) = self.finish(id)
                && !call.abandoned
            {
                // Retryable: the caller resends under its own request key, which the ledger settles at most
                // once (section 4.6); kelmod itself never sends it again.
                self.refuse(
                    call.source,
                    &call.caller_id,
                    "E_CORE_RESTARTED",
                    "The core restarted during the call; its effect is unknown.",
                    true,
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
        if state.hard.is_some_and(|hard| bytes > hard) {
            state.restarting = true;
            let _ = state.supervisor.send(Command::Kill);
            return;
        }
        if state.soft.is_some_and(|soft| bytes > soft) {
            match state.over_soft_since {
                None => {
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
                Some(_) => {
                    let idle = !self.calls.values().any(|c| c.core == core && c.dispatched);
                    if idle {
                        self.graceful_restart(core);
                    }
                }
            }
        } else {
            state.over_soft_since = None;
        }
    }

    fn graceful_restart(&mut self, core: usize) {
        let state = &mut self.cores[core];
        state.restarting = true;
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
        // Heartbeats bypass the mailboxes; a core that misses too many is killed and restarted.
        if now.duration_since(self.last_heartbeat) >= self.spec.heartbeat {
            self.last_heartbeat = now;
            for core in 0..self.cores.len() {
                let state = &self.cores[core];
                if !state.ready || state.restarting {
                    continue;
                }
                if state.pings.len() as u32 >= self.spec.heartbeat_misses {
                    self.cores[core].restarting = true;
                    let _ = self.cores[core].supervisor.send(Command::Kill);
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

    fn shutdown(&mut self) {
        self.shutting_down = true;
        for core in &self.cores {
            if let Some(out) = &core.out {
                send(
                    out,
                    &json!({"v": 2, "kind": "call", "id": hex(0), "op": "admin.stop", "lane": "admin",
                                  "deadline_ms": 30000, "body_type": "json"}),
                    &json!({}),
                );
            }
            let _ = core.supervisor.send(Command::Stop);
        }
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
               "calls": self.calls.len(), "cores": cores, "p95_ms": p95})
    }
}
