//! The embedding provider process and its JSON line channel.

use std::collections::BTreeMap;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU8, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, TryLockError, mpsc};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

use crate::embed::error;

const MAX_LINE: usize = 16 << 20;

#[derive(Clone)]
pub struct Config {
    argv: Vec<String>,
    env: BTreeMap<String, String>,
}

impl Config {
    /// Validates the optional command and its complete environment.
    pub fn parse(command: Option<&str>, env: Option<&str>) -> Result<Option<Self>, String> {
        let Some(command) = command else {
            return if env.is_some() {
                Err("--provider-env needs --provider-command".into())
            } else {
                Ok(None)
            };
        };
        let argv: Vec<String> =
            serde_json::from_str(command).map_err(|e| format!("--provider-command: {e}"))?;
        if argv.is_empty()
            || argv.iter().any(String::is_empty)
            || !Path::new(&argv[0]).is_absolute()
        {
            return Err(
                "--provider-command needs non-empty strings and an absolute executable".into(),
            );
        }
        let env = match env {
            Some(text) => serde_json::from_str::<BTreeMap<String, String>>(text)
                .map_err(|e| format!("--provider-env: {e}"))?,
            None => BTreeMap::new(),
        };
        Ok(Some(Self { argv, env }))
    }
}

#[derive(Default)]
struct GateState {
    busy: bool,
    queries: usize,
    stopped: bool,
}

#[derive(Default)]
struct Gate {
    state: Mutex<GateState>,
    changed: Condvar,
}

struct Turn<'a>(&'a Gate);

impl Drop for Turn<'_> {
    fn drop(&mut self) {
        let mut state = self.0.state.lock().unwrap_or_else(|e| e.into_inner());
        state.busy = false;
        self.0.changed.notify_all();
    }
}

impl Gate {
    /// Waits for the pipe, giving waiting queries precedence over passages.
    fn enter(&self, query: bool, deadline: Instant) -> Result<Turn<'_>, Value> {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if query {
            state.queries += 1;
        }
        loop {
            if state.stopped {
                if query {
                    state.queries -= 1;
                    self.changed.notify_all();
                }
                return Err(unavailable("provider is stopping", true));
            }
            if Instant::now() >= deadline {
                if query {
                    state.queries -= 1;
                    self.changed.notify_all();
                }
                return Err(timeout());
            }
            if !state.busy && (query || state.queries == 0) {
                if query {
                    state.queries -= 1;
                }
                state.busy = true;
                return Ok(Turn(self));
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            state = self
                .changed
                .wait_timeout(state, remaining)
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
    }

    /// Refuses new turns and wakes waiters.
    fn shutdown(&self) {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.stopped = true;
        self.changed.notify_all();
    }
}

#[derive(Debug)]
enum Line {
    Data(Vec<u8>),
    TooLong,
    Io,
}

struct Process {
    child: Child,
    input: Option<ChildStdin>,
    lines: mpsc::Receiver<Line>,
}

impl Process {
    /// Starts the provider with private pipes and a new process group.
    fn start(config: &Config) -> io::Result<Self> {
        let mut command = Command::new(&config.argv[0]);
        command
            .args(&config.argv[1..])
            .env_clear()
            .envs(&config.env)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .process_group(0);
        let mut child = command.spawn()?;
        let input = child.stdin.take();
        let output = child.stdout.take().expect("piped stdout");
        let (tx, lines) = mpsc::channel();
        // The reader is never joined: it ends when the pipe closes, and a descendant that left the group can
        // hold the pipe open long after the provider is gone.
        thread::spawn(move || read_lines(BufReader::new(output), tx));
        Ok(Self {
            child,
            input,
            lines,
        })
    }

    /// Closes the channel, kills the group and reaps the child.
    fn stop(&mut self, poisoned: bool) {
        drop(self.input.take());
        let pid = self.child.id();
        if !poisoned {
            let until = Instant::now() + Duration::from_secs(3);
            while Instant::now() < until {
                match self.child.try_wait() {
                    Ok(Some(_)) | Err(_) => break,
                    Ok(None) => thread::sleep(Duration::from_millis(10)),
                }
            }
        }
        kelmo_runtime::sys::kill_group(pid);
        // A provider that left its own group is still this process's child and is killed by its pid.
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Reads bounded lines without allowing an unterminated line to grow indefinitely.
fn read_lines<R: BufRead>(mut input: R, sender: mpsc::Sender<Line>) {
    let mut line = Vec::new();
    loop {
        let buffer = match input.fill_buf() {
            Ok(buffer) if !buffer.is_empty() => buffer,
            _ => {
                let _ = sender.send(Line::Io);
                return;
            }
        };
        let end = buffer.iter().position(|byte| *byte == b'\n');
        let count = end.unwrap_or(buffer.len());
        if line.len() + count > MAX_LINE {
            let _ = sender.send(Line::TooLong);
            return;
        }
        line.extend_from_slice(&buffer[..count]);
        input.consume(count + usize::from(end.is_some()));
        if end.is_some() && sender.send(Line::Data(std::mem::take(&mut line))).is_err() {
            return;
        }
    }
}

/// Checks an answer and returns it only when it names this request.
fn answer(line: Line, id: &str) -> Result<Option<Value>, Value> {
    match line {
        Line::Io => Err(unavailable("provider pipe closed", true)),
        Line::TooLong => Err(protocol("answer exceeds 16 MiB")),
        Line::Data(bytes) => {
            let value: Value =
                serde_json::from_slice(&bytes).map_err(|_| protocol("invalid JSON answer"))?;
            let answer_id = value
                .as_object()
                .and_then(|object| object.get("id"))
                .and_then(Value::as_str)
                .ok_or_else(|| protocol("answer has no string id"))?;
            Ok((answer_id == id).then_some(value))
        }
    }
}

/// Builds a protocol request line.
fn request(id: &str, kind: &str, texts: &Value) -> Vec<u8> {
    let mut line = json!({"id": id, "kind": kind, "texts": texts})
        .to_string()
        .into_bytes();
    line.push(b'\n');
    line
}

/// Creates a random request identifier.
fn request_id() -> io::Result<String> {
    let mut bytes = [0u8; 16];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// Returns the requested timeout or the purpose's default, capped at 300 seconds.
fn duration(body: &Value, kind: &str) -> Duration {
    let default = if kind == "passage" { 60.0 } else { 5.0 };
    let seconds = body["timeout"]
        .as_f64()
        .filter(|number| number.is_finite() && *number > 0.0)
        .unwrap_or(default)
        .min(300.0);
    Duration::from_secs_f64(seconds)
}

fn unavailable(message: &str, retryable: bool) -> Value {
    error(
        "E_EMBEDDING",
        &format!("embedding_unavailable: {message}"),
        retryable,
    )
}

fn timeout() -> Value {
    error("E_EMBEDDING", "timeout: provider deadline expired", true)
}

fn protocol(message: &str) -> Value {
    error("E_EMBEDDING", &format!("protocol_error: {message}"), true)
}

pub struct Provider {
    config: Option<Config>,
    gate: Gate,
    process: Mutex<Option<Process>>,
    pid: AtomicU32,
    stopping: AtomicBool,
    starts: AtomicU64,
    failures: AtomicU64,
}

impl Provider {
    /// Creates an initially stopped provider.
    pub fn new(config: Option<Config>) -> Self {
        Self {
            config,
            gate: Gate::default(),
            process: Mutex::new(None),
            pid: AtomicU32::new(0),
            stopping: AtomicBool::new(false),
            starts: AtomicU64::new(0),
            failures: AtomicU64::new(0),
        }
    }

    pub fn configured(&self) -> bool {
        self.config.is_some()
    }

    /// Returns provider configuration and lifetime counters.
    pub fn status(&self) -> Value {
        if let Ok(mut slot) = self.process.try_lock() {
            let exited = slot
                .as_mut()
                .is_some_and(|process| matches!(process.child.try_wait(), Ok(Some(_))));
            if exited && let Some(mut process) = slot.take() {
                process.stop(true);
                self.pid.store(0, Ordering::Release);
            }
        }
        json!({
            "configured": self.configured(),
            "running": self.pid.load(Ordering::Acquire) != 0,
            "starts": self.starts.load(Ordering::Relaxed),
            "failures": self.failures.load(Ordering::Relaxed),
        })
    }

    /// Relays a request, serializing access to the provider pipe.
    pub fn call(&self, body: &Value) -> Result<Value, Value> {
        let kind = body["purpose"]
            .as_str()
            .filter(|kind| matches!(*kind, "health" | "query" | "passage"))
            .ok_or_else(|| error("E_INPUT", "embed.embed needs a valid purpose", false))?;
        let texts = body["texts"]
            .as_array()
            .filter(|texts| texts.iter().all(Value::is_string))
            .ok_or_else(|| error("E_INPUT", "embed.embed needs an array of strings", false))?;
        let Some(config) = &self.config else {
            return Err(unavailable("no provider configured", false));
        };
        let deadline = Instant::now() + duration(body, kind);
        let _turn = self.gate.enter(kind != "passage", deadline)?;
        let mut slot = self.process.lock().unwrap_or_else(|e| e.into_inner());
        if self.stopping.load(Ordering::Acquire) {
            return Err(unavailable("provider is stopping", true));
        }
        // A provider that exited between calls is replaced now, so its death costs no caller a failed call.
        let exited = slot
            .as_mut()
            .is_some_and(|process| matches!(process.child.try_wait(), Ok(Some(_))));
        if exited && let Some(mut process) = slot.take() {
            process.stop(true);
            self.pid.store(0, Ordering::Release);
        }
        if slot.is_none() {
            match Process::start(config) {
                Ok(process) => {
                    self.pid.store(process.child.id(), Ordering::Release);
                    self.starts.fetch_add(1, Ordering::Relaxed);
                    *slot = Some(process);
                }
                Err(e) => {
                    self.failures.fetch_add(1, Ordering::Relaxed);
                    return Err(unavailable(&format!("cannot start provider: {e}"), true));
                }
            }
        }
        if self.stopping.load(Ordering::Acquire) {
            if let Some(mut process) = slot.take() {
                process.stop(false);
            }
            self.pid.store(0, Ordering::Release);
            return Err(unavailable("provider is stopping", true));
        }
        let id = request_id()
            .map_err(|e| unavailable(&format!("cannot create request id: {e}"), true))?;
        let line = request(&id, kind, &Value::Array(texts.clone()));
        if Instant::now() >= deadline {
            return Err(timeout());
        }
        let process = slot.as_mut().expect("provider started");
        let input = process.input.take().expect("provider stdin");
        // The writer can block on a full stdin; the reader drains stdout independently.
        // The writer is never joined: killing the group normally unblocks it, but a descendant that left the
        // group can hold the pipe, and then the writer ends whenever that descendant does.
        let (tx, rx) = mpsc::channel();
        let write_state = Arc::new(AtomicU8::new(0));
        let writer_state = Arc::clone(&write_state);
        thread::spawn(move || {
            let mut input = input;
            let result = if writer_state
                .compare_exchange(0, 1, Ordering::AcqRel, Ordering::Acquire)
                .is_ok()
            {
                input.write_all(&line)
            } else {
                Ok(())
            };
            let _ = tx.send((input, result));
        });
        let mut early = None;
        // Whether the call ended for a reason that says nothing against the provider (a stop or the deadline);
        // a closed pipe or an invalid answer means the provider is gone or broken.
        let mut harmless = false;
        let result = loop {
            if self.stopping.load(Ordering::Acquire) {
                harmless = true;
                break Err(unavailable("provider is stopping", true));
            }
            if Instant::now() >= deadline {
                harmless = true;
                break Err(timeout());
            }
            match rx.try_recv() {
                Ok((input, Ok(()))) => {
                    process.input = Some(input);
                    break match early {
                        Some(value) => Ok(value),
                        None => receive(&process.lines, &id, deadline, &self.stopping),
                    };
                }
                Ok((_, Err(e))) => break Err(unavailable(&format!("write failed: {e}"), true)),
                Err(mpsc::TryRecvError::Disconnected) => {
                    break Err(unavailable("provider writer stopped", true));
                }
                Err(mpsc::TryRecvError::Empty) => {}
            }
            match process.lines.recv_timeout(
                deadline
                    .saturating_duration_since(Instant::now())
                    .min(Duration::from_millis(5)),
            ) {
                Ok(line) => match answer(line, &id) {
                    Ok(Some(value)) => early = Some(value),
                    Ok(None) => {}
                    Err(e) => break Err(e),
                },
                Err(mpsc::RecvTimeoutError::Disconnected) => {
                    break Err(unavailable("provider reader stopped", true));
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
            }
        };
        // Only a harmless end can leave the provider in place, and only when the writer never touched the pipe.
        let cancelled = result.is_err()
            && harmless
            && write_state
                .compare_exchange(0, 2, Ordering::AcqRel, Ordering::Acquire)
                .is_ok();
        let graceful = self.stopping.load(Ordering::Acquire) && process.input.is_some();
        if result.is_err() && !graceful && !cancelled {
            self.failures.fetch_add(1, Ordering::Relaxed);
            drop(process.input.take());
            kelmo_runtime::sys::kill_group(process.child.id());
        }
        // A cancelled writer wrote nothing and hands the pipe back at once; one that does not is treated as lost.
        let mut returned = false;
        if cancelled && let Ok((input, _)) = rx.recv_timeout(Duration::from_secs(1)) {
            process.input = Some(input);
            returned = true;
        }
        if result.is_err()
            && !(cancelled && returned)
            && let Some(mut process) = slot.take()
        {
            process.stop(!graceful);
            self.pid.store(0, Ordering::Release);
        }
        result
    }

    /// Stops a provider only when no request is using it.
    pub fn trim(&self) -> bool {
        let mut gate = self.gate.state.lock().unwrap_or_else(|e| e.into_inner());
        if gate.busy || gate.stopped {
            return false;
        }
        gate.busy = true;
        drop(gate);
        let _turn = Turn(&self.gate);
        let mut slot = match self.process.try_lock() {
            Ok(slot) => slot,
            Err(TryLockError::WouldBlock) => return false,
            Err(TryLockError::Poisoned(e)) => e.into_inner(),
        };
        if let Some(mut process) = slot.take() {
            process.stop(false);
            self.pid.store(0, Ordering::Release);
            true
        } else {
            false
        }
    }

    /// Stops the provider and prevents any subsequent request from starting it.
    pub fn stop(&self) {
        self.stopping.store(true, Ordering::Release);
        self.gate.shutdown();
        let mut slot = self.process.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(mut process) = slot.take() {
            process.stop(false);
        }
        self.pid.store(0, Ordering::Release);
    }
}

/// Waits for the matching answer and drops valid answers to other requests.
fn receive(
    lines: &mpsc::Receiver<Line>,
    id: &str,
    deadline: Instant,
    stopping: &AtomicBool,
) -> Result<Value, Value> {
    loop {
        if stopping.load(Ordering::Acquire) {
            return Err(unavailable("provider is stopping", true));
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err(timeout());
        }
        match lines.recv_timeout(remaining.min(Duration::from_millis(10))) {
            Ok(line) => {
                if let Some(value) = answer(line, id)? {
                    return Ok(value);
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err(unavailable("provider reader stopped", true));
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::sync::Arc;

    fn config(argv: &[&str]) -> Config {
        Config {
            argv: argv.iter().map(|item| (*item).to_owned()).collect(),
            env: BTreeMap::new(),
        }
    }

    fn body(seconds: f64) -> Value {
        json!({"purpose": "query", "texts": ["hello"], "timeout": seconds})
    }

    fn reason(value: &Value) -> &str {
        value["message"].as_str().unwrap()
    }

    #[test]
    fn configuration_validates_command_and_environment() {
        assert!(Config::parse(None, Some("{}")).is_err());
        assert!(Config::parse(Some("[]"), None).is_err());
        assert!(Config::parse(Some("[\"sh\"]"), None).is_err());
        assert!(Config::parse(Some("[\"/bin/sh\", \"\"]"), None).is_err());
        assert!(Config::parse(Some("[\"/bin/sh\"]"), Some("{\"A\":1}")).is_err());
        assert!(Config::parse(Some("[\"/bin/sh\"]"), Some("{\"A\":\"b\"}")).is_ok());
    }

    #[test]
    fn request_and_matching_answers_preserve_the_protocol() {
        let line = request("abcd", "query", &json!(["hello"]));
        assert_eq!(line.last(), Some(&b'\n'));
        assert_eq!(
            serde_json::from_slice::<Value>(&line).unwrap(),
            json!({"id": "abcd", "kind": "query", "texts": ["hello"]})
        );
        let (tx, rx) = mpsc::channel();
        tx.send(Line::Data(br#"{"id":"late","value":1}"#.to_vec()))
            .unwrap();
        tx.send(Line::Data(br#"{"id":"abcd","error":"kept"}"#.to_vec()))
            .unwrap();
        assert_eq!(
            receive(
                &rx,
                "abcd",
                Instant::now() + Duration::from_secs(1),
                &AtomicBool::new(false),
            )
            .unwrap(),
            json!({"id":"abcd","error":"kept"})
        );
    }

    #[test]
    fn invalid_answers_and_oversized_lines_are_protocol_errors() {
        for bytes in [b"invalid".as_slice(), b"[]", br#"{"id":7}"#] {
            assert!(
                reason(&answer(Line::Data(bytes.to_vec()), "a").unwrap_err())
                    .starts_with("protocol_error:")
            );
        }
        let (tx, rx) = mpsc::channel();
        read_lines(Cursor::new(vec![b'x'; MAX_LINE + 1]), tx);
        assert!(matches!(rx.recv().unwrap(), Line::TooLong));
    }

    #[test]
    fn line_reader_splits_lines_and_reports_eof() {
        let (tx, rx) = mpsc::channel();
        read_lines(Cursor::new(b"one\ntwo\n"), tx);
        assert!(matches!(rx.recv().unwrap(), Line::Data(bytes) if bytes.as_slice() == b"one"));
        assert!(matches!(rx.recv().unwrap(), Line::Data(bytes) if bytes.as_slice() == b"two"));
        assert!(matches!(rx.recv().unwrap(), Line::Io));
    }

    #[test]
    fn deadlines_have_defaults_and_a_cap() {
        assert_eq!(duration(&json!({}), "query"), Duration::from_secs(5));
        assert_eq!(
            duration(&json!({"timeout": 0}), "passage"),
            Duration::from_secs(60)
        );
        assert_eq!(
            duration(&json!({"timeout": 999}), "query"),
            Duration::from_secs(300)
        );
        assert_eq!(
            duration(&json!({"timeout": 0.25}), "query"),
            Duration::from_millis(250)
        );
    }

    #[test]
    fn missing_command_reports_start_failure() {
        let provider = Provider::new(Some(config(&["/nonexistent"])));
        assert!(
            reason(&provider.call(&body(1.0)).unwrap_err()).starts_with("embedding_unavailable:")
        );
        assert_eq!(provider.status()["failures"], 1);
    }

    #[test]
    fn cat_echoes_requests_and_trim_stops_it() {
        let provider = Provider::new(Some(config(&["/bin/cat"])));
        for _ in 0..2 {
            let answer = provider.call(&body(1.0)).unwrap();
            assert_eq!(answer["kind"], "query");
            assert_eq!(answer["texts"], json!(["hello"]));
            assert_eq!(answer["id"].as_str().unwrap().len(), 32);
        }
        assert_eq!(provider.status()["starts"], 1);
        assert!(provider.trim());
        assert_eq!(provider.status()["running"], false);
    }

    #[test]
    fn exited_provider_is_restarted_on_next_call() {
        let provider = Provider::new(Some(config(&["/bin/sh", "-c", "exit 0"])));
        for expected in 1..=2 {
            assert!(
                reason(&provider.call(&body(1.0)).unwrap_err())
                    .starts_with("embedding_unavailable:")
            );
            assert_eq!(provider.status()["starts"], expected);
            assert_eq!(provider.status()["running"], false);
        }
    }

    #[test]
    fn provider_that_exited_between_calls_is_replaced_without_a_failed_call() {
        let provider = Provider::new(Some(config(&[
            "/bin/sh",
            "-c",
            "read line; echo \"$line\"",
        ])));
        for _ in 1..=2 {
            assert_eq!(provider.call(&body(2.0)).unwrap()["kind"], "query");
            // The one-shot child ends after its answer. `status` is not asked meanwhile: it reaps an exited
            // provider itself, and this test is about the check the call makes.
            thread::sleep(Duration::from_millis(300));
        }
        assert_eq!(provider.status()["starts"], 2);
        assert_eq!(provider.status()["failures"], 0);
    }

    #[test]
    fn descendant_outside_the_group_cannot_hold_a_call_or_stop() {
        // The descendant leaves the provider's group and keeps both pipes open for five seconds.
        let provider = Provider::new(Some(config(&[
            "/usr/bin/perl",
            "-MPOSIX",
            "-e",
            "if (fork() == 0) { POSIX::setsid(); sleep 5; exit 0 } sleep 30",
        ])));
        // A deadline long enough to start the provider and begin the write on a loaded machine: one spent before
        // the request is sent leaves the fresh provider running, which is not what this test is about.
        let body = json!({"purpose": "query", "texts": ["x".repeat(1024 * 1024)], "timeout": 1.0});
        let start = Instant::now();
        assert!(reason(&provider.call(&body).unwrap_err()).starts_with("timeout:"));
        assert_eq!(provider.status()["running"], false);
        provider.stop();
        assert!(start.elapsed() < Duration::from_secs(4));
    }

    #[test]
    fn timeout_kills_and_reaps_unresponsive_provider() {
        let provider = Provider::new(Some(config(&["/bin/sh", "-c", "/bin/sleep 30"])));
        let start = Instant::now();
        // Long enough that the request is written before the deadline, as above.
        assert!(reason(&provider.call(&body(1.0)).unwrap_err()).starts_with("timeout:"));
        assert!(start.elapsed() < Duration::from_secs(3));
        assert_eq!(provider.status()["running"], false);
        assert_eq!(provider.status()["failures"], 1);
    }

    #[test]
    fn timeout_during_large_write_kills_provider() {
        let provider = Provider::new(Some(config(&["/bin/sh", "-c", "/bin/sleep 30"])));
        // Long enough that the write has begun and blocks on the full pipe when the deadline passes.
        let body = json!({"purpose": "query", "texts": ["x".repeat(1024 * 1024)], "timeout": 1.0});
        let start = Instant::now();
        assert!(reason(&provider.call(&body).unwrap_err()).starts_with("timeout:"));
        assert!(start.elapsed() < Duration::from_secs(3));
        assert_eq!(provider.status()["running"], false);
    }

    #[test]
    fn stop_reaps_provider() {
        let provider = Provider::new(Some(config(&["/bin/sh", "-c", "/bin/cat >/dev/null"])));
        let process = Process::start(provider.config.as_ref().unwrap()).unwrap();
        let pid = process.child.id();
        provider.pid.store(pid, Ordering::Release);
        *provider.process.lock().unwrap() = Some(process);
        provider.stop();
        assert!(!kelmo_runtime::sys::alive(pid as i32));
        assert_eq!(provider.status()["running"], false);
    }

    #[test]
    fn waiting_timeout_leaves_active_provider_alone() {
        let provider = Arc::new(Provider::new(Some(config(&[
            "/bin/sh",
            "-c",
            "/bin/sleep 30",
        ]))));
        let active = Arc::clone(&provider);
        let thread = thread::spawn(move || active.call(&body(0.5)));
        let deadline = Instant::now() + Duration::from_secs(1);
        while provider.pid.load(Ordering::Acquire) == 0 && Instant::now() < deadline {
            thread::yield_now();
        }
        let pid = provider.pid.load(Ordering::Acquire);
        assert_ne!(pid, 0);
        assert!(reason(&provider.call(&body(0.03)).unwrap_err()).starts_with("timeout:"));
        assert_eq!(provider.pid.load(Ordering::Acquire), pid);
        assert!(reason(&thread.join().unwrap().unwrap_err()).starts_with("timeout:"));
    }

    #[test]
    fn priority_goes_to_waiting_queries() {
        let gate = Arc::new(Gate::default());
        let turn = gate
            .enter(false, Instant::now() + Duration::from_secs(1))
            .unwrap();
        let (tx, rx) = mpsc::channel();
        let query_gate = Arc::clone(&gate);
        let query_tx = tx.clone();
        let query = thread::spawn(move || {
            let _turn = query_gate
                .enter(true, Instant::now() + Duration::from_secs(1))
                .unwrap();
            query_tx.send("query").unwrap();
        });
        let deadline = Instant::now() + Duration::from_secs(1);
        while gate.state.lock().unwrap().queries == 0 && Instant::now() < deadline {
            thread::yield_now();
        }
        let passage_gate = Arc::clone(&gate);
        let passage = thread::spawn(move || {
            let _turn = passage_gate
                .enter(false, Instant::now() + Duration::from_secs(1))
                .unwrap();
            tx.send("passage").unwrap();
        });
        drop(turn);
        assert_eq!(rx.recv_timeout(Duration::from_secs(1)).unwrap(), "query");
        assert_eq!(rx.recv_timeout(Duration::from_secs(1)).unwrap(), "passage");
        query.join().unwrap();
        passage.join().unwrap();
    }
}
