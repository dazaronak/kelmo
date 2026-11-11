//! `kelmod serve`: take the instance lock, bind the protocol socket, start the cores, and route until a
//! signal or a spent restart budget ends the daemon. An ephemeral daemon (`--ephemeral`, started by the CLI
//! when none runs for the root) also ends once no client has been connected for a short grace period.

use std::sync::Arc;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tokio::signal::unix::{SignalKind, signal};
use tokio::sync::mpsc::unbounded_channel;
use tokio::sync::watch;

use crate::gateway::{Gateway, next_connection};
use crate::ipc;
use crate::router::{Beat, Command, Event, ROUTER_DRAIN, Router};
use crate::spec::Spec;
use crate::supervisor::{read_frames, start_all, write_frames};

/// Exit code when another daemon already owns the root.
pub const EXIT_BUSY: i32 = 75;
/// A spec the daemon cannot serve (sysexits EX_CONFIG): a service manager should not restart it in a loop.
pub const EXIT_CONFIG: i32 = 78;
/// The router task ended before a shutdown asked it to (a panic), or stopped handling events for
/// `ROUTER_STALL`: the service manager restarts the daemon.
pub const EXIT_ROUTER: i32 = 70;
/// A router that handled no event for this long is stuck. The daemon stops itself, so launchd, which has no
/// watchdog, restarts it too; under systemd the withheld pings restart it sooner.
const ROUTER_STALL: Duration = Duration::from_secs(60);
const TICK: Duration = Duration::from_millis(50);
/// The open-file limit kelmod asks for (macOS's `OPEN_MAX`), and the least it serves its connection cap with.
const OPEN_FILES: u64 = 10240;
const MIN_OPEN_FILES: u64 = 2048;
/// How long an ephemeral daemon waits for its first client, and after its last one leaves.
const FIRST_CLIENT_GRACE: Duration = Duration::from_secs(10);
const LAST_CLIENT_GRACE: Duration = Duration::from_secs(2);

/// Connected clients and when that number last changed, for the ephemeral exit.
struct Presence {
    open: u64,
    changed: Instant,
    seen: bool,
}

impl Presence {
    fn idle(&self) -> bool {
        let grace = if self.seen {
            LAST_CLIENT_GRACE
        } else {
            FIRST_CLIENT_GRACE
        };
        self.open == 0 && self.changed.elapsed() >= grace
    }
}

/// Sends a systemd notification when the service manager asked for them; a no-op otherwise.
fn notify(state: &str) {
    let Some(path) = std::env::var_os("NOTIFY_SOCKET") else {
        return;
    };
    let Ok(socket) = std::os::unix::net::UnixDatagram::unbound() else {
        return;
    };
    let bytes = path.as_encoded_bytes();
    if let Some(name) = bytes.strip_prefix(b"@") {
        #[cfg(target_os = "linux")]
        {
            use std::os::linux::net::SocketAddrExt;
            if let Ok(address) = std::os::unix::net::SocketAddr::from_abstract_name(name) {
                let _ = socket.send_to_addr(state.as_bytes(), &address);
            }
        }
        let _ = name;
    } else {
        let _ = socket.send_to(state.as_bytes(), &path);
    }
}

fn watchdog_interval() -> Option<Duration> {
    let usec: u64 = std::env::var("WATCHDOG_USEC").ok()?.parse().ok()?;
    // A zero period would panic tokio's interval; no service manager asks for one.
    Some(Duration::from_micros(usec / 2)).filter(|interval| !interval.is_zero())
}

/// A watchdog ping vouches for the router, not only for this loop: it goes out while the router has handled an
/// event within one ping interval (its tick arrives every 50 ms), so a stuck router gets the daemon restarted.
fn router_alive(age: Duration, interval: Duration) -> bool {
    age < interval
}

/// Starts a front end (the HTTP MCP endpoint) once the router runs; the daemon waits for its task on a stop.
pub type FrontEnd = Box<dyn FnOnce(Gateway) -> tokio::task::JoinHandle<()> + Send>;
/// Prepares a front end (binds its port) once the instance lock is held, so a second daemon of the same root
/// fails on the lock (busy) and never on the port.
pub type Prepare = Box<dyn FnOnce() -> Result<FrontEnd, String> + Send>;

/// Runs the daemon to its end and returns the process exit code.
pub async fn serve(spec: Spec, ephemeral: bool) -> i32 {
    serve_with(spec, ephemeral, None).await
}

/// `serve` with an optional front end beside the protocol socket.
pub async fn serve_with(spec: Spec, ephemeral: bool, front: Option<Prepare>) -> i32 {
    if ephemeral && front.is_some() {
        // An ephemeral daemon counts socket clients only and would stop under live HTTP sessions.
        eprintln!("E_CONFIG: an ephemeral daemon serves no HTTP endpoint");
        return EXIT_CONFIG;
    }
    let _lock = match ipc::InstanceLock::acquire(&spec.state_dir.join("daemon.lock")) {
        Ok(lock) => lock,
        Err(message) => {
            eprintln!("E_BUSY: {message}");
            return EXIT_BUSY;
        }
    };
    let open_files = crate::sys::raise_open_files(OPEN_FILES);
    if open_files < MIN_OPEN_FILES {
        eprintln!(
            "kelmod: the open-file limit is {open_files}; connections past it wait for a free descriptor"
        );
    }
    let runtime = match spec
        .runtime_dir
        .clone()
        .map(Ok)
        .unwrap_or_else(|| ipc::runtime_dir(&spec.root))
    {
        Ok(path) => path,
        Err(error) => {
            eprintln!("E_RUNTIME: {error}");
            return 1;
        }
    };
    let listener = match ipc::bind(&runtime) {
        Ok(listener) => listener,
        Err(error) => {
            eprintln!("E_RUNTIME: {}: {error}", runtime.display());
            return 1;
        }
    };
    let front = match front.map(|prepare| prepare()).transpose() {
        Ok(front) => front,
        Err(message) => {
            eprintln!("E_CONFIG: {message}");
            let _ = std::fs::remove_file(ipc::socket_path(&runtime));
            return EXIT_CONFIG;
        }
    };
    let origin = spec.origin.clone();
    let (events, receiver) = unbounded_channel();
    let (fatal_tx, mut fatal_rx) = unbounded_channel::<i32>();
    let drain = spec.drain_timeout;
    let (supervisors, supervisor_tasks) = start_all(&spec, &events, &fatal_tx);
    // Kept so the daemon can stop the cores itself when the router that normally does is lost.
    let core_commands = supervisors.clone();
    let router = Router::new(spec, supervisors);
    let (ready_tx, mut ready_rx) = unbounded_channel();
    let beat = Beat::new();
    let mut router_task = tokio::spawn(router.run(receiver, ready_tx, beat.clone()));
    let (stopping_tx, stopping_rx) = watch::channel(false);
    let front_task = front.map(|start| {
        start(Gateway::new(
            events.clone(),
            stopping_rx,
            origin,
            runtime.clone(),
        ))
    });

    let presence = Arc::new(Mutex::new(Presence {
        open: 0,
        changed: Instant::now(),
        seen: false,
    }));
    let accept_presence = presence.clone();
    let accept_events = events.clone();
    tokio::spawn(async move {
        loop {
            let Ok((stream, _)) = listener.accept().await else {
                continue;
            };
            if !ipc::same_user(&stream) {
                continue;
            }
            let conn = next_connection();
            if let Ok(mut seen) = accept_presence.lock() {
                seen.open += 1;
                seen.seen = true;
                seen.changed = Instant::now();
            }
            let leave = accept_presence.clone();
            let (reader, writer) = stream.into_split();
            let (out, queue) = unbounded_channel();
            if accept_events.send(Event::ClientOpen { conn, out }).is_err() {
                return;
            }
            tokio::spawn(write_frames(writer, queue));
            let forward = accept_events.clone();
            tokio::spawn(async move {
                let frames = forward.clone();
                read_frames(reader, move |header, body| {
                    frames
                        .send(Event::ClientFrame { conn, header, body })
                        .is_ok()
                })
                .await;
                let _ = forward.send(Event::ClientClosed { conn });
                if let Ok(mut seen) = leave.lock() {
                    seen.open = seen.open.saturating_sub(1);
                    seen.changed = Instant::now();
                }
            });
        }
    });

    let tick_events = events.clone();
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(TICK);
        loop {
            interval.tick().await;
            if tick_events.send(Event::Tick).is_err() {
                return;
            }
        }
    });

    let mut terminate = signal(SignalKind::terminate()).expect("signal handler");
    let mut interrupt = signal(SignalKind::interrupt()).expect("signal handler");
    let ping = watchdog_interval();
    let mut watchdog = ping.map(tokio::time::interval);
    let mut presence_check = ephemeral.then(|| tokio::time::interval(Duration::from_millis(100)));
    let mut stall_check = tokio::time::interval(Duration::from_secs(1));
    // Set when the router ended or stalled: it can neither drain the calls nor stop the cores.
    let mut router_lost = false;
    let code = loop {
        tokio::select! {
            Some(()) = ready_rx.recv() => notify("READY=1"),
            Some(code) = fatal_rx.recv() => break code,
            _ = terminate.recv() => break 0,
            _ = interrupt.recv() => break 0,
            ended = &mut router_task => {
                router_lost = true;
                match ended {
                    Err(error) if error.is_panic() => eprintln!("kelmod: the router panicked; stopping"),
                    _ => eprintln!("kelmod: the router stopped before a shutdown; stopping"),
                }
                break EXIT_ROUTER;
            }
            _ = stall_check.tick() => {
                let age = beat.age();
                if !router_alive(age, ROUTER_STALL) {
                    eprintln!("kelmod: the router has handled no event for {} ms; stopping", age.as_millis());
                    router_lost = true;
                    break EXIT_ROUTER;
                }
            }
            _ = async {
                match watchdog.as_mut() {
                    Some(interval) => { interval.tick().await; }
                    None => std::future::pending::<()>().await,
                }
            } => {
                let age = beat.age();
                if ping.is_some_and(|interval| router_alive(age, interval)) {
                    notify("WATCHDOG=1");
                } else {
                    eprintln!("kelmod: the router has handled no event for {} ms; no watchdog ping", age.as_millis());
                }
            }
            idle = async {
                match presence_check.as_mut() {
                    Some(interval) => {
                        interval.tick().await;
                        presence.lock().map(|seen| seen.idle()).unwrap_or(false)
                    }
                    None => std::future::pending::<bool>().await,
                }
            } => if idle { break 0 },
        }
    };
    notify("STOPPING=1");
    // The router drains first: it refuses new calls as retryable and waits for the admitted ones, whose replies
    // still travel through the open front end. Only then does the front end close its connections.
    let _ = events.send(Event::Shutdown);
    if !router_lost {
        let _ = tokio::time::timeout(ROUTER_DRAIN + Duration::from_secs(3), &mut router_task).await;
    }
    // A drained router has already sent every core `admin.stop` and its supervisor a stop. One that was lost (or
    // did not finish in time) did not: the supervisors get the stop here, and a core that does not end by its drain
    // deadline is killed. A second stop never moves the first deadline.
    for commands in &core_commands {
        let _ = commands.send(Command::Stop);
    }
    let _ = stopping_tx.send(true);
    if let Some(task) = front_task {
        let _ = tokio::time::timeout(Duration::from_secs(2), task).await;
    }
    // Every core gets its drain before the runtime is dropped: dropping it kills the children at once.
    let drained = async {
        for task in supervisor_tasks {
            let _ = task.await;
        }
    };
    let _ = tokio::time::timeout(drain + Duration::from_secs(2), drained).await;
    let _ = std::fs::remove_file(ipc::socket_path(&runtime));
    code
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    use super::*;
    use crate::router::Beat;

    #[test]
    fn a_fresh_beat_is_young() {
        let beat = Beat::new();
        assert!(beat.age() < Duration::from_secs(1));
    }

    #[test]
    fn a_marked_beat_ages_while_the_router_is_quiet() {
        let beat = Beat::new();
        beat.mark();
        std::thread::sleep(Duration::from_millis(120));
        let age = beat.age();
        assert!(age >= Duration::from_millis(100), "age was {age:?}");
        assert!(age < Duration::from_secs(2), "age was {age:?}");
    }

    #[test]
    fn marking_again_makes_the_beat_young_again() {
        let beat = Beat::new();
        beat.mark();
        std::thread::sleep(Duration::from_millis(120));
        let before = beat.age();
        beat.mark();
        assert!(beat.age() < before);
    }

    #[test]
    fn a_clone_of_the_beat_sees_the_marks_of_the_original() {
        let original = Beat::new();
        let clone = original.clone();
        std::thread::sleep(Duration::from_millis(120));
        let before = clone.age();
        original.mark();
        assert!(clone.age() < before);
    }

    #[test]
    fn a_router_within_the_interval_keeps_the_watchdog_ping() {
        assert!(router_alive(Duration::from_secs(1), Duration::from_secs(2)));
    }

    #[test]
    fn a_router_exactly_at_the_interval_withholds_the_watchdog_ping() {
        assert!(!router_alive(
            Duration::from_secs(2),
            Duration::from_secs(2)
        ));
    }

    #[test]
    fn a_stale_beat_withholds_the_watchdog_ping() {
        assert!(!router_alive(
            Duration::from_secs(3),
            Duration::from_secs(2)
        ));
    }

    #[test]
    fn a_router_that_has_just_handled_an_event_keeps_the_watchdog_ping() {
        assert!(router_alive(Duration::ZERO, Duration::from_secs(15)));
    }

    #[test]
    fn the_router_exit_code_is_not_the_config_code_systemd_refuses_to_restart() {
        assert_eq!(EXIT_ROUTER, 70);
        assert_eq!(EXIT_CONFIG, 78);
        assert_ne!(EXIT_ROUTER, EXIT_CONFIG);
    }
}
