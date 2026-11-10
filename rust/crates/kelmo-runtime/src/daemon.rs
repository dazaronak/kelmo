//! `kelmod serve`: take the instance lock, bind the protocol socket, start the cores, and route until a
//! signal or a spent restart budget ends the daemon. An ephemeral daemon (`--ephemeral`, started by the CLI
//! when none runs for the root) also ends once no client has been connected for a short grace period.

use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use tokio::signal::unix::{SignalKind, signal};
use tokio::sync::mpsc::unbounded_channel;

use crate::ipc;
use crate::router::{Event, Router};
use crate::spec::Spec;
use crate::supervisor::{read_frames, start_all, write_frames};

/// Exit code when another daemon already owns the root.
pub const EXIT_BUSY: i32 = 75;
const TICK: Duration = Duration::from_millis(50);
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

static CONNECTIONS: AtomicU64 = AtomicU64::new(1);

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
    Some(Duration::from_micros(usec / 2))
}

/// Runs the daemon to its end and returns the process exit code.
pub async fn serve(spec: Spec, ephemeral: bool) -> i32 {
    let _lock = match ipc::InstanceLock::acquire(&spec.state_dir.join("daemon.lock")) {
        Ok(lock) => lock,
        Err(message) => {
            eprintln!("E_BUSY: {message}");
            return EXIT_BUSY;
        }
    };
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
    let (events, receiver) = unbounded_channel();
    let (fatal_tx, mut fatal_rx) = unbounded_channel::<i32>();
    let supervisors = start_all(&spec, &events, &fatal_tx);
    let router = Router::new(spec, supervisors);
    let (ready_tx, mut ready_rx) = unbounded_channel();
    let router_task = tokio::spawn(router.run(receiver, ready_tx));

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
            let conn = CONNECTIONS.fetch_add(1, Ordering::Relaxed);
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
    let mut watchdog = watchdog_interval().map(tokio::time::interval);
    let mut presence_check = ephemeral.then(|| tokio::time::interval(Duration::from_millis(100)));
    let code = loop {
        tokio::select! {
            Some(()) = ready_rx.recv() => notify("READY=1"),
            Some(code) = fatal_rx.recv() => break code,
            _ = terminate.recv() => break 0,
            _ = interrupt.recv() => break 0,
            _ = async {
                match watchdog.as_mut() {
                    Some(interval) => { interval.tick().await; }
                    None => std::future::pending::<()>().await,
                }
            } => notify("WATCHDOG=1"),
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
    let _ = events.send(Event::Shutdown);
    let _ = tokio::time::timeout(Duration::from_secs(35), router_task).await;
    let _ = std::fs::remove_file(ipc::socket_path(&runtime));
    code
}
