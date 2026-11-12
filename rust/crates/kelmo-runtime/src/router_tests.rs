use std::time::{Duration, Instant};

use serde_json::{Value, json};
use tokio::sync::mpsc::{UnboundedReceiver, unbounded_channel};

use super::*;

const MIB: u64 = 1 << 20;

fn spec(heartbeat_ms: u64, drain_ms: u64) -> Spec {
    let lanes = json!({"fast": {"limit": 4, "queue": 16}});
    Spec::from_json(&json!({
        "root": "/nonexistent/kelmo-router-test",
        "origin": {"client_id": "test"},
        "cores": [
            {"name": "state", "argv": ["state"], "lanes": lanes, "budget": {"soft_mb": 1}},
            {"name": "archive", "argv": ["archive"], "lanes": lanes},
        ],
        "operations": [
            {"name": "state.echo", "lanes": ["fast"], "replay": "read"},
            {"name": "archive.echo", "lanes": ["fast"], "replay": "read"},
        ],
        "heartbeat_ms": heartbeat_ms,
        "drain_timeout_ms": drain_ms,
    }))
    .expect("spec")
}

fn id(n: u64) -> String {
    format!("{n:032x}")
}

/// A router whose cores are started and ready, with their outbound queues and supervisor commands.
struct Harness {
    router: Router,
    outs: Vec<UnboundedReceiver<Vec<u8>>>,
    commands: Vec<UnboundedReceiver<Command>>,
    client: Option<UnboundedReceiver<Vec<u8>>>,
}

const STATE: usize = 0;
const ARCHIVE: usize = 1;

impl Harness {
    fn new(spec: Spec) -> Self {
        let mut senders = Vec::new();
        let mut commands = Vec::new();
        for _ in &spec.cores {
            let (tx, rx) = unbounded_channel();
            senders.push(tx);
            commands.push(rx);
        }
        let mut harness = Harness {
            router: Router::new(spec, senders),
            outs: Vec::new(),
            commands,
            client: None,
        };
        for core in 0..harness.commands.len() {
            let (out, rx) = unbounded_channel();
            harness.outs.push(rx);
            harness.step(Event::CoreStarted {
                core,
                out,
                pid: 1000 + core as u32,
            });
            harness.step(Event::CoreFrame {
                core,
                header: json!({"v": 2, "kind": "hello", "id": id(900 + core as u64),
                               "body_type": "json", "operations": []}),
                body: Value::Null,
            });
            harness.frames(core);
        }
        harness
    }

    /// Handles an event as the router's task does.
    fn step(&mut self, event: Event) {
        self.router.handle(event);
        if self.router.shutting_down {
            self.router.stop_when_drained();
        }
    }

    fn frames(&mut self, core: usize) -> Vec<(Value, Value)> {
        drain(&mut self.outs[core])
    }

    fn commands(&mut self, core: usize) -> Vec<Command> {
        let mut seen = Vec::new();
        while let Ok(command) = self.commands[core].try_recv() {
            seen.push(command);
        }
        seen
    }

    fn sample(&mut self, core: usize, bytes: u64) {
        self.step(Event::Memory {
            core,
            bytes: Some(bytes),
        });
    }

    fn reply(&mut self, core: usize, to: &str, error: Option<Value>, body: Value) {
        self.step(Event::CoreFrame {
            core,
            header: kelmo_wire::header::reply(to, error),
            body,
        });
    }

    /// Over the soft budget twice: a trim, then a recycle. Returns the recycle's id.
    fn ask_recycle(&mut self) -> String {
        self.sample(STATE, 2 * MIB);
        assert_eq!(ops(&self.frames(STATE)), ["admin.trim"]);
        self.sample(STATE, 2 * MIB);
        let frames = self.frames(STATE);
        assert_eq!(ops(&frames), ["admin.recycle"]);
        assert_eq!(frames[0].0["deadline_ms"], 10000);
        assert!(self.router.cores[STATE].recycle_pending.is_some());
        frames[0].0["id"].as_str().expect("id").to_owned()
    }

    /// The supervisor brought the core back: it exited, started again and said hello.
    fn restart(&mut self, core: usize) {
        self.step(Event::CoreExited { core });
        let (out, rx) = unbounded_channel();
        self.outs[core] = rx;
        self.step(Event::CoreStarted {
            core,
            out,
            pid: 2000 + core as u32,
        });
        self.step(Event::CoreFrame {
            core,
            header: json!({"v": 2, "kind": "hello", "id": id(950 + core as u64),
                           "body_type": "json", "operations": []}),
            body: Value::Null,
        });
        self.frames(core);
        self.commands(core);
    }

    /// The state core agrees to a recycle and comes back from the planned restart.
    fn planned_restart(&mut self) {
        let asked = self.ask_recycle();
        self.reply(STATE, &asked, None, json!({"recycle": true}));
        assert_eq!(self.commands(STATE), [Command::Restart]);
        self.restart(STATE);
    }

    fn client_call(&mut self, op: &str, caller: u64) {
        if self.client.is_none() {
            let (out, rx) = unbounded_channel();
            self.client = Some(rx);
            self.step(Event::ClientOpen { conn: 1, out });
            self.step(Event::ClientFrame {
                conn: 1,
                header: json!({"v": 2, "kind": "hello", "id": id(800), "body_type": "json"}),
                body: Value::Null,
            });
            self.client_frames();
        }
        self.step(Event::ClientFrame {
            conn: 1,
            header: json!({"v": 2, "kind": "call", "id": id(caller), "op": op,
                           "deadline_ms": 60000, "body_type": "json"}),
            body: json!({}),
        });
    }

    fn client_frames(&mut self) -> Vec<(Value, Value)> {
        drain(self.client.as_mut().expect("client"))
    }
}

fn drain(queue: &mut UnboundedReceiver<Vec<u8>>) -> Vec<(Value, Value)> {
    let mut reader = kelmo_wire::FrameReader::new();
    let mut frames = Vec::new();
    while let Ok(bytes) = queue.try_recv() {
        frames.extend(reader.feed(&bytes).expect("a valid frame"));
    }
    frames
}

fn ops(frames: &[(Value, Value)]) -> Vec<String> {
    frames
        .iter()
        .filter(|(header, _)| header["kind"] == "call")
        .map(|(header, _)| header["op"].as_str().unwrap_or("").to_owned())
        .collect()
}

fn ago(span: Duration) -> Instant {
    Instant::now().checked_sub(span).expect("a past instant")
}

fn past() -> Instant {
    ago(Duration::from_millis(1))
}

#[test]
fn callers_stop_before_the_cores_they_call() {
    assert_eq!(stop_groups(&["state", "archive", "embed"]), [0, 1, 2]);
    assert_eq!(stop_groups(&["embed", "archive", "state"]), [2, 1, 0]);
    assert_eq!(stop_groups(&["edge"]), [0]);
    assert_eq!(stop_groups(&["state", "embed"]), [0, 1]);
}

#[test]
fn a_later_group_drains_at_most_ten_seconds_and_every_group_adds_its_slack() {
    // state (group 0) and archive (group 1): the callers' drain, then at most 10 s, each plus 1 s of slack.
    assert_eq!(group_drain(&spec(5000, 45_000), 0), Duration::from_secs(45));
    assert_eq!(group_drain(&spec(5000, 45_000), 1), Duration::from_secs(10));
    assert_eq!(stop_budget(&spec(5000, 45_000)), Duration::from_secs(57));
    // A drain shorter than ten seconds is not stretched for a later group.
    assert_eq!(group_drain(&spec(5000, 5_000), 1), Duration::from_secs(5));
    assert_eq!(stop_budget(&spec(5000, 5_000)), Duration::from_secs(12));
}

#[test]
fn the_callers_drain_is_capped_at_forty_five_seconds() {
    assert_eq!(spec(5000, 120_000).drain_timeout, Duration::from_secs(45));
}

#[test]
fn a_core_that_agrees_to_a_recycle_is_restarted_gracefully() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    let asked = harness.ask_recycle();
    harness.reply(STATE, &asked, None, json!({"recycle": true}));
    assert_eq!(ops(&harness.frames(STATE)), ["admin.stop"]);
    assert_eq!(harness.commands(STATE), [Command::Restart]);
    assert!(harness.router.cores[STATE].restarting);
    assert!(harness.router.cores[STATE].recycle_pending.is_none());
}

#[test]
fn a_declining_core_is_kept_its_calls_resume_and_it_is_not_asked_again_soon() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    let asked = harness.ask_recycle();
    harness.client_call("state.echo", 1);
    assert!(
        ops(&harness.frames(STATE)).is_empty(),
        "no call goes out while a recycle is pending"
    );
    harness.reply(
        STATE,
        &asked,
        None,
        json!({"recycle": false, "busy": ["job"]}),
    );
    let frames = harness.frames(STATE);
    assert_eq!(ops(&frames), ["state.echo"]);
    assert!(harness.commands(STATE).is_empty());
    let state = &harness.router.cores[STATE];
    assert!(!state.restarting && state.recycle_pending.is_none());
    let retry = state.recycle_retry_at.expect("a retry time");
    assert!(retry >= Instant::now() + Duration::from_secs(29));
    // The echo is answered, so the core is idle again; within 30 s it is still not asked.
    let routed = frames[0].0["id"].as_str().expect("id").to_owned();
    harness.reply(STATE, &routed, None, json!({"echo": true}));
    let answered = harness.client_frames();
    assert_eq!(answered.len(), 1);
    assert_eq!(answered[0].0["id"], id(1));
    harness.sample(STATE, 2 * MIB);
    assert!(ops(&harness.frames(STATE)).is_empty());
    harness.router.cores[STATE].recycle_retry_at = Some(past());
    harness.sample(STATE, 2 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.recycle"]);
}

#[test]
fn an_error_reply_to_a_recycle_is_a_decline() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    let asked = harness.ask_recycle();
    let error = json!({"code": "E_PROTOCOL", "message": "admin.recycle is not served here.",
                       "retryable": false});
    harness.reply(STATE, &asked, Some(error), Value::Null);
    assert!(harness.frames(STATE).is_empty());
    assert!(harness.commands(STATE).is_empty());
    let state = &harness.router.cores[STATE];
    assert!(state.recycle_pending.is_none() && state.recycle_retry_at.is_some());
    assert!(!state.restarting);
}

#[test]
fn no_answer_by_the_deadline_is_a_decline() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    let asked = harness.ask_recycle();
    harness.router.cores[STATE].recycle_deadline = Some(past());
    harness.step(Event::Tick);
    let state = &harness.router.cores[STATE];
    assert!(state.recycle_pending.is_none() && state.recycle_retry_at.is_some());
    // A late consent is not acted on.
    harness.reply(STATE, &asked, None, json!({"recycle": true}));
    assert!(harness.frames(STATE).is_empty());
    assert!(harness.commands(STATE).is_empty());
    assert!(!harness.router.cores[STATE].restarting);
}

#[test]
fn heartbeats_go_on_while_a_recycle_is_pending() {
    let mut harness = Harness::new(spec(0, 5_000));
    harness.ask_recycle();
    harness.step(Event::Tick);
    let pinged = ops(&harness.frames(STATE));
    assert_eq!(pinged, ["admin.ping"]);
}

#[test]
fn a_core_that_exits_while_a_recycle_is_pending_is_no_longer_pending() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.ask_recycle();
    harness.step(Event::CoreExited { core: STATE });
    assert!(harness.router.cores[STATE].recycle_pending.is_none());
    assert!(harness.router.recycles.is_empty());
}

#[test]
fn a_shutdown_stops_the_caller_first_and_routes_its_calls_while_it_drains() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.step(Event::Shutdown);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.stop"]);
    assert_eq!(harness.commands(STATE), [Command::Stop]);
    assert!(harness.frames(ARCHIVE).is_empty());
    assert!(harness.commands(ARCHIVE).is_empty());

    // The draining state core calls the archive, which is still up; the reply comes back.
    harness.step(Event::CoreFrame {
        core: STATE,
        header: json!({"v": 2, "kind": "call", "id": id(42), "op": "archive.echo",
                       "deadline_ms": 5000, "body_type": "json"}),
        body: json!({"n": 1}),
    });
    let forwarded = harness.frames(ARCHIVE);
    assert_eq!(ops(&forwarded), ["archive.echo"]);
    let routed = forwarded[0].0["id"].as_str().expect("id").to_owned();
    harness.reply(ARCHIVE, &routed, None, json!({"echo": 1}));
    let replies = harness.frames(STATE);
    assert_eq!(replies.len(), 1);
    assert_eq!(replies[0].0["id"], id(42));
    assert_eq!(replies[0].0["ok"], true);
    assert_eq!(replies[0].1, json!({"echo": 1}));
    assert!(harness.commands(ARCHIVE).is_empty());

    harness.step(Event::CoreExited { core: STATE });
    assert!(
        harness.frames(ARCHIVE).is_empty(),
        "the exit alone is not the end"
    );
    harness.step(Event::CoreStopped { core: STATE });
    assert_eq!(ops(&harness.frames(ARCHIVE)), ["admin.stop"]);
    assert_eq!(harness.commands(ARCHIVE), [Command::Stop]);
    assert!(!harness.router.stopped);

    harness.step(Event::CoreExited { core: ARCHIVE });
    harness.step(Event::CoreStopped { core: ARCHIVE });
    assert!(harness.router.stopped);
}

#[test]
fn a_group_past_its_drain_lets_the_next_group_stop() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.step(Event::Shutdown);
    harness.frames(STATE);
    harness.router.stopping = Some((0, ago(Duration::from_secs(10))));
    harness.step(Event::Tick);
    assert_eq!(ops(&harness.frames(ARCHIVE)), ["admin.stop"]);
}

#[test]
fn a_core_started_after_its_stop_is_stopped_too() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.step(Event::Shutdown);
    harness.frames(STATE);
    harness.step(Event::CoreExited { core: STATE });
    let (out, rx) = unbounded_channel();
    harness.outs[STATE] = rx;
    harness.step(Event::CoreStarted {
        core: STATE,
        out,
        pid: 2000,
    });
    assert_eq!(ops(&harness.frames(STATE)), ["admin.stop"]);
}

#[test]
fn a_stopping_daemon_asks_no_recycle() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    // A call the state core made to the archive holds the daemon in its drain window: no core is told to stop yet,
    // and the state core, which serves nothing, is idle.
    harness.step(Event::CoreFrame {
        core: STATE,
        header: json!({"v": 2, "kind": "call", "id": id(42), "op": "archive.echo",
                       "deadline_ms": 5000, "body_type": "json"}),
        body: json!({"n": 1}),
    });
    assert_eq!(ops(&harness.frames(ARCHIVE)), ["archive.echo"]);
    harness.sample(STATE, 2 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.trim"]);
    harness.step(Event::Shutdown);
    assert!(
        !harness.router.cores[STATE].stop_sent,
        "the drain window is open"
    );
    harness.sample(STATE, 2 * MIB);
    assert!(ops(&harness.frames(STATE)).is_empty());
    assert!(harness.router.cores[STATE].recycle_pending.is_none());
}

#[test]
fn a_core_whose_stop_went_out_is_never_pinged() {
    let mut harness = Harness::new(spec(0, 5_000));
    harness.step(Event::Shutdown);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.stop"]);
    harness.step(Event::Tick);
    assert!(
        ops(&harness.frames(STATE)).is_empty(),
        "a draining core's drain is never cut short by missed pings"
    );
    assert_eq!(ops(&harness.frames(ARCHIVE)), ["admin.ping"]);
}

#[test]
fn a_consent_that_arrives_during_the_shutdown_releases_the_held_calls_without_a_restart() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    let asked = harness.ask_recycle();
    harness.client_call("state.echo", 1);
    assert!(ops(&harness.frames(STATE)).is_empty());
    harness.step(Event::Shutdown);
    harness.reply(STATE, &asked, None, json!({"recycle": true}));
    assert_eq!(ops(&harness.frames(STATE)), ["state.echo"]);
    assert!(
        !harness.commands(STATE).contains(&Command::Restart),
        "the core stops with its group, not by a restart"
    );
    let state = &harness.router.cores[STATE];
    assert!(!state.restarting && state.recycle_pending.is_none());
}

#[test]
fn a_recycle_left_unanswered_releases_the_held_calls() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.ask_recycle();
    harness.client_call("state.echo", 1);
    assert!(ops(&harness.frames(STATE)).is_empty());
    harness.router.cores[STATE].recycle_deadline = Some(past());
    harness.step(Event::Tick);
    assert_eq!(ops(&harness.frames(STATE)), ["state.echo"]);
}

#[test]
fn a_hard_breach_while_a_recycle_is_pending_kills_the_core() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.router.cores[STATE].hard = Some(4 * MIB);
    harness.ask_recycle();
    harness.sample(STATE, 5 * MIB);
    assert_eq!(harness.commands(STATE), [Command::Kill]);
    assert!(harness.router.cores[STATE].restarting);
    harness.step(Event::CoreExited { core: STATE });
    assert!(harness.router.cores[STATE].recycle_pending.is_none());
    assert!(harness.router.recycles.is_empty());
}

#[test]
fn a_core_serving_a_call_is_not_asked_to_recycle() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.client_call("state.echo", 1);
    let routed = harness.frames(STATE);
    assert_eq!(ops(&routed), ["state.echo"]);
    harness.sample(STATE, 2 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.trim"]);
    harness.sample(STATE, 2 * MIB);
    assert!(ops(&harness.frames(STATE)).is_empty());
    assert!(harness.router.cores[STATE].recycle_pending.is_none());
    let call = routed[0].0["id"].as_str().expect("id").to_owned();
    harness.reply(STATE, &call, None, json!({"echo": true}));
    harness.sample(STATE, 2 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.recycle"]);
}

#[test]
fn a_restarted_core_starts_with_no_recycle_state_and_is_asked_again_when_over() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    let asked = harness.ask_recycle();
    harness.reply(
        STATE,
        &asked,
        None,
        json!({"recycle": false, "busy": ["job"]}),
    );
    assert!(harness.router.cores[STATE].recycle_retry_at.is_some());
    // It crashed on its own: no planned restart, so the soft budget is not pinned afterwards.
    harness.restart(STATE);
    let state = &harness.router.cores[STATE];
    assert!(state.recycle_retry_at.is_none() && !state.recycle_declined);
    assert!(state.over_soft_since.is_none() && !state.planned);
    harness.sample(STATE, 2 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.trim"]);
    harness.sample(STATE, 2 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.recycle"]);
    assert_eq!(harness.router.cores[STATE].soft, Some(MIB));
}

#[test]
fn a_core_over_soft_right_after_a_planned_restart_is_pinned_not_recycled() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.router.cores[STATE].hard = Some(8 * MIB);
    harness.planned_restart();
    harness.sample(STATE, 2 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.trim"]);
    harness.sample(STATE, 2 * MIB);
    assert!(
        ops(&harness.frames(STATE)).is_empty(),
        "no second recycle: the restart did not help"
    );
    assert!(harness.commands(STATE).is_empty());
    let state = &harness.router.cores[STATE];
    assert_eq!(state.soft, Some(2 * MIB / 4 * 5));
    assert_eq!(state.soft_base, Some(MIB));
    let budget = &harness.router.status()["cores"][STATE]["budget"];
    assert_eq!(budget["pinned"], true);
    assert_eq!(budget["spec_soft_bytes"], MIB);
    // Under the pin the core is left alone.
    harness.sample(STATE, 2 * MIB);
    assert!(ops(&harness.frames(STATE)).is_empty());
    // Growth past the pin is trimmed and recycled as before.
    harness.sample(STATE, 3 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.trim"]);
    harness.sample(STATE, 3 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.recycle"]);
}

#[test]
fn a_kill_after_a_planned_restart_is_not_a_planned_restart() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.router.cores[STATE].hard = Some(4 * MIB);
    harness.planned_restart();
    harness.sample(STATE, 5 * MIB);
    assert_eq!(harness.commands(STATE), [Command::Kill]);
    harness.restart(STATE);
    harness.sample(STATE, 2 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.trim"]);
    harness.sample(STATE, 2 * MIB);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.recycle"]);
    assert_eq!(harness.router.cores[STATE].soft, Some(MIB));
}

#[test]
fn the_pin_stays_below_the_hard_budget_and_recycles_resume_once_the_restart_is_old() {
    let mut harness = Harness::new(spec(60_000, 5_000));
    harness.router.cores[STATE].hard = Some(3 * MIB);
    harness.planned_restart();
    let near_hard = 3 * MIB - MIB / 10;
    harness.sample(STATE, near_hard);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.trim"]);
    harness.sample(STATE, near_hard);
    assert!(ops(&harness.frames(STATE)).is_empty());
    assert_eq!(harness.router.cores[STATE].soft, Some(3 * MIB / 10 * 9));
    // Still over the capped pin: trimmed again, then asked to recycle once the restart is PIN_SAMPLES samples old.
    harness.sample(STATE, near_hard);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.trim"]);
    harness.sample(STATE, near_hard);
    assert_eq!(ops(&harness.frames(STATE)), ["admin.recycle"]);
}
