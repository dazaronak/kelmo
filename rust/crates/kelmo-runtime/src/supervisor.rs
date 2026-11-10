//! One task per core: spawn it with its channel on descriptor 3, carry frames both ways, restart it under a
//! budget, and sample its memory.

use std::collections::VecDeque;
use std::os::fd::{AsRawFd, OwnedFd};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::process::Command as Process;
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender, unbounded_channel};

use crate::router::{Command, Event};
use crate::spec::{CoreSpec, Spec};
use crate::sys;

/// Exit code of kelmod when a core restarts more often than its budget allows: the service manager then
/// restarts the whole tree with back-off.
pub const EXIT_RESTART_BUDGET: i32 = 70;

pub struct Budget {
    max: usize,
    window: Duration,
    starts: VecDeque<Instant>,
}

impl Budget {
    pub fn new(max: usize, window: Duration) -> Self {
        Budget {
            max,
            window,
            starts: VecDeque::new(),
        }
    }

    /// Records a restart; false when it is over the budget.
    pub fn restart(&mut self, now: Instant) -> bool {
        while self
            .starts
            .front()
            .is_some_and(|t| now.duration_since(*t) > self.window)
        {
            self.starts.pop_front();
        }
        self.starts.push_back(now);
        self.starts.len() <= self.max
    }
}

/// Reads frames from a channel and forwards each as an event; ends at end of file or on a refused frame.
pub async fn read_frames<R, F>(mut reader: R, mut forward: F)
where
    R: AsyncReadExt + Unpin,
    F: FnMut(serde_json::Value, serde_json::Value) -> bool,
{
    let mut decoder = kelmo_wire::FrameReader::new();
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let read = match reader.read(&mut buffer).await {
            Ok(0) | Err(_) => return,
            Ok(read) => read,
        };
        match decoder.feed(&buffer[..read]) {
            Ok(frames) => {
                for (header, body) in frames {
                    if !forward(header, body) {
                        return;
                    }
                }
            }
            Err(_) => return,
        }
    }
}

/// Writes queued frames to a channel until the queue closes or the peer goes away.
pub async fn write_frames<W>(mut writer: W, mut queue: UnboundedReceiver<Vec<u8>>)
where
    W: AsyncWriteExt + Unpin,
{
    while let Some(bytes) = queue.recv().await {
        if writer.write_all(&bytes).await.is_err() {
            return;
        }
    }
    let _ = writer.shutdown().await;
}

fn spawn(core: &CoreSpec) -> std::io::Result<(tokio::process::Child, UnixStream)> {
    let (parent, child) = std::os::unix::net::UnixStream::pair()?;
    parent.set_nonblocking(true)?;
    let child: OwnedFd = child.into();
    let mut command = Process::new(&core.argv[0]);
    command
        .args(&core.argv[1..])
        .env_clear()
        .envs(&core.env)
        .stdin(std::process::Stdio::null())
        .kill_on_drop(true);
    sys::new_process_group(&mut command);
    sys::map_descriptors(&mut command, vec![(child.as_raw_fd(), 3)]);
    let process = command.spawn();
    // The child has its own copy on descriptor 3; this process's copy closes here.
    drop(child);
    Ok((process?, UnixStream::from_std(parent)?))
}

/// Runs one core until `Stop`. Exits the process with EXIT_RESTART_BUDGET when the restart budget is spent.
pub struct Supervised {
    pub index: usize,
    pub core: CoreSpec,
    pub restart_max: usize,
    pub restart_window: Duration,
    pub drain: Duration,
    pub sample: Duration,
    /// Where a core records the process groups of its executor children while they run.
    pub executors: PathBuf,
}

/// Kills the executor process groups a core left recorded (it crashed or was killed mid-call) and removes
/// their records, so no model call outlives the core that started it.
pub fn kill_leftovers(directory: &Path) {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        if let Some(pgid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
            .filter(|pgid| *pgid > 1)
        {
            sys::kill_group(pgid);
        }
        let _ = std::fs::remove_file(entry.path());
    }
}

pub async fn supervise(
    supervised: Supervised,
    events: UnboundedSender<Event>,
    mut commands: UnboundedReceiver<Command>,
    fatal: UnboundedSender<i32>,
) {
    let Supervised {
        index,
        core,
        restart_max,
        restart_window,
        drain,
        sample,
        executors,
    } = supervised;
    let mut budget = Budget::new(restart_max, restart_window);
    let mut first = true;
    loop {
        if !first && !budget.restart(Instant::now()) {
            let _ = fatal.send(EXIT_RESTART_BUDGET);
            return;
        }
        first = false;
        kill_leftovers(&executors);
        let (mut child, stream) = match spawn(&core) {
            Ok(spawned) => spawned,
            Err(_) => {
                tokio::time::sleep(Duration::from_millis(200)).await;
                let _ = events.send(Event::CoreExited { core: index });
                continue;
            }
        };
        let pid = child.id().unwrap_or(0);
        let (reader, writer) = stream.into_split();
        let (out, queue) = unbounded_channel();
        let _ = events.send(Event::CoreStarted {
            core: index,
            out,
            pid,
        });
        let writer_task = tokio::spawn(write_frames(writer, queue));
        let forward = events.clone();
        let reader_task = tokio::spawn(read_frames(reader, move |header, body| {
            forward
                .send(Event::CoreFrame {
                    core: index,
                    header,
                    body,
                })
                .is_ok()
        }));
        let mut sampler = tokio::time::interval(sample);
        sampler.tick().await;
        let mut stopping = false;
        let mut stop_deadline: Option<tokio::time::Instant> = None;
        loop {
            let deadline = stop_deadline;
            tokio::select! {
                _ = child.wait() => break,
                _ = sampler.tick() => {
                    let _ = events.send(Event::Memory { core: index, bytes: sys::resident_bytes(pid) });
                }
                command = commands.recv() => match command {
                    Some(Command::Kill) => {
                        sys::kill_group(pid);
                        let _ = child.kill().await;
                    }
                    Some(Command::Restart) => {
                        stop_deadline = Some(tokio::time::Instant::now() + drain);
                    }
                    Some(Command::Stop) | None => {
                        stopping = true;
                        stop_deadline = Some(tokio::time::Instant::now() + drain);
                    }
                },
                _ = async {
                    match deadline {
                        Some(at) => tokio::time::sleep_until(at).await,
                        None => std::future::pending::<()>().await,
                    }
                } => {
                    sys::kill_group(pid);
                    let _ = child.kill().await;
                }
            }
        }
        reader_task.abort();
        writer_task.abort();
        sys::kill_group(pid);
        kill_leftovers(&executors);
        let _ = events.send(Event::CoreExited { core: index });
        if stopping {
            return;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}

/// Builds the supervisor tasks for every core of the spec.
pub fn start_all(
    spec: &Spec,
    events: &UnboundedSender<Event>,
    fatal: &UnboundedSender<i32>,
) -> Vec<UnboundedSender<Command>> {
    let mut senders = Vec::new();
    for (index, core) in spec.cores.iter().cloned().enumerate() {
        let (tx, rx) = unbounded_channel();
        senders.push(tx);
        let supervised = Supervised {
            index,
            core,
            restart_max: spec.restart_max,
            restart_window: spec.restart_window,
            drain: spec.drain_timeout,
            sample: spec.sample,
            executors: spec.state_dir.join("executors"),
        };
        tokio::spawn(supervise(supervised, events.clone(), rx, fatal.clone()));
    }
    senders
}
