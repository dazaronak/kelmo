//! One task per core: spawn it with its channel on descriptor 3, carry frames both ways, restart it under a
//! budget, and sample its memory.

use std::collections::VecDeque;
use std::os::fd::{AsRawFd, OwnedFd};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixStream;
use tokio::process::Command as Process;
use tokio::sync::mpsc::error::TryRecvError;
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
/// Returns why the channel ended when that was not a plain end of stream: a read error or a frame that does
/// not decode, which the caller reports, since a core's channel then restarts.
pub async fn read_frames<R, F>(mut reader: R, mut forward: F) -> Option<String>
where
    R: AsyncReadExt + Unpin,
    F: FnMut(serde_json::Value, serde_json::Value) -> bool,
{
    let mut decoder = kelmo_wire::FrameReader::new();
    let mut buffer = vec![0u8; 64 * 1024];
    loop {
        let read = match reader.read(&mut buffer).await {
            Ok(0) => return None,
            Err(error) => return Some(format!("read failed: {error}")),
            Ok(read) => read,
        };
        match decoder.feed(&buffer[..read]) {
            Ok(frames) => {
                for (header, body) in frames {
                    if !forward(header, body) {
                        return None;
                    }
                }
            }
            Err(error) => return Some(format!("invalid frame: {error}")),
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
/// their records, so no model call outlives the core that started it. A group whose id now leads a process
/// started after the record (another start time) is left alone.
pub fn kill_leftovers(directory: &Path) {
    let Ok(entries) = std::fs::read_dir(directory) else {
        return;
    };
    for entry in entries.flatten() {
        let pgid = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<u32>().ok())
            .filter(|pgid| *pgid > 1);
        let recorded = std::fs::read_to_string(entry.path())
            .ok()
            .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
            .map(|value| value["start"].as_str().map(str::to_owned));
        // A leader that is gone leaves its group's id unusable for a new group while members remain, so
        // the kill reaches only the executor's survivors; a live leader must be the recorded process.
        if let (Some(pgid), Some(start)) = (pgid, recorded) {
            let same = match sys::process_start(pgid) {
                None => true,
                Some(running) => start.as_deref() == Some(running.as_str()),
            };
            if same {
                sys::kill_group(pgid);
            }
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
    let mut charge_restart = false;
    // A closed command channel (every sender gone) stays closed: read it once, not on every turn of the loop.
    // While the daemon runs it keeps a sender of its own, so the channel stays open and a stop arrives as
    // `Command::Stop`, from the router or, when the router is lost, from the daemon itself.
    let mut commands_open = true;
    loop {
        if charge_restart && !budget.restart(Instant::now()) {
            let _ = fatal.send(EXIT_RESTART_BUDGET);
            return;
        }
        charge_restart = true;
        kill_leftovers(&executors);
        let (mut child, stream) = match spawn(&core) {
            Ok(spawned) => spawned,
            Err(_) => {
                tokio::time::sleep(Duration::from_millis(200)).await;
                let _ = events.send(Event::CoreExited { core: index });
                if stop_waiting(&mut commands) {
                    return;
                }
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
        // The channel is read until the core closes it, even once the router is gone: a stopping core answers
        // `admin.stop` before it drains, and that answer must not end the read (and with it the core).
        let mut reader_task = tokio::spawn(read_frames(reader, move |header, body| {
            let _ = forward.send(Event::CoreFrame {
                core: index,
                header,
                body,
            });
            true
        }));
        let mut sampler = tokio::time::interval(sample);
        sampler.tick().await;
        let mut stopping = false;
        let mut planned_restart = false;
        let mut stop_deadline: Option<tokio::time::Instant> = None;
        loop {
            let deadline = stop_deadline;
            tokio::select! {
                status = child.wait() => {
                    planned_restart &= status.as_ref().is_ok_and(|status| status.success());
                    if let Ok(status) = status
                        && !stopping
                        && !status.success()
                    {
                        eprintln!("kelmod: core {} exited ({status}); restarting it", core.name);
                    }
                    // A reply the core wrote just before it ended is still in the channel: read it out first.
                    if !reader_task.is_finished() {
                        let _ = tokio::time::timeout(Duration::from_millis(200), &mut reader_task).await;
                    }
                    break;
                }
                ended = &mut reader_task, if !reader_task.is_finished() => {
                    let reason = ended.ok().flatten();
                    if stop_deadline.is_some() && reason.is_none() {
                        // A core asked to stop or restart closes its channel as it ends: wait for its exit or
                        // the deadline.
                        continue;
                    }
                    // A core that is ending closes its channel first: a moment for it to exit names its status.
                    let exited = if reason.is_none() {
                        tokio::time::timeout(Duration::from_secs(1), child.wait()).await.ok().and_then(Result::ok)
                    } else {
                        None
                    };
                    let then = if stopping { "" } else { "; restarting it" };
                    match (reason, exited) {
                        (Some(reason), _) => eprintln!(
                            "kelmod: the channel of core {} stopped ({reason}){then}", core.name),
                        (None, Some(status)) => eprintln!(
                            "kelmod: core {} exited ({status}){then}", core.name),
                        (None, None) => eprintln!(
                            "kelmod: the channel of core {} stopped{then}", core.name),
                    }
                    sys::kill_group(pid);
                    let _ = child.kill().await;
                    planned_restart = false;
                    break;
                }
                _ = sampler.tick() => {
                    let _ = events.send(Event::Memory { core: index, bytes: sys::tree_resident_bytes(pid) });
                }
                command = commands.recv(), if commands_open => match command {
                    Some(Command::Kill) => {
                        planned_restart = false;
                        sys::kill_group(pid);
                        let _ = child.kill().await;
                    }
                    Some(Command::Restart) => {
                        if !stopping {
                            planned_restart = true;
                            stop_deadline.get_or_insert(tokio::time::Instant::now() + drain);
                        }
                    }
                    command => {
                        if command.is_none() {
                            commands_open = false;
                        }
                        // The first Stop gets a full drain even during a planned restart. Repeated Stops
                        // keep that deadline so shutdown cannot be postponed indefinitely.
                        if !stopping {
                            let deadline = tokio::time::Instant::now() + drain;
                            stop_deadline = Some(stop_deadline.map_or(deadline, |old| old.max(deadline)));
                            stopping = true;
                        }
                        // Closing kelmod's half of the channel is a stop the core sees even when the router that
                        // would send `admin.stop` is stuck: the core reads end of file, drains and exits.
                        writer_task.abort();
                    }
                },
                _ = async {
                    match deadline {
                        Some(at) => tokio::time::sleep_until(at).await,
                        None => std::future::pending::<()>().await,
                    }
                } => {
                    planned_restart = false;
                    sys::kill_group(pid);
                    let _ = child.kill().await;
                    // Spent: the killed core's exit ends the loop, and a past deadline must not spin it.
                    stop_deadline = None;
                }
            }
        }
        charge_restart = !planned_restart;
        reader_task.abort();
        writer_task.abort();
        sys::kill_group(pid);
        kill_leftovers(&executors);
        let _ = events.send(Event::CoreExited { core: index });
        // A stop that came while the core was ending, or during the pause before the next start, must not let a
        // new one start.
        if stopping || stop_waiting(&mut commands) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
        if stop_waiting(&mut commands) {
            return;
        }
    }
}

/// True when a stop waits in the command channel, or every sender is gone. Other commands are dropped: no core
/// runs to apply them to.
fn stop_waiting(commands: &mut UnboundedReceiver<Command>) -> bool {
    loop {
        match commands.try_recv() {
            Ok(Command::Stop) | Err(TryRecvError::Disconnected) => return true,
            Ok(_) => {}
            Err(TryRecvError::Empty) => return false,
        }
    }
}

/// Builds the supervisor tasks for every core of the spec: their command senders, and the tasks themselves,
/// which the daemon waits for on a stop so every core drains before the process ends.
pub fn start_all(
    spec: &Spec,
    events: &UnboundedSender<Event>,
    fatal: &UnboundedSender<i32>,
) -> (
    Vec<UnboundedSender<Command>>,
    Vec<tokio::task::JoinHandle<()>>,
) {
    let mut senders = Vec::new();
    let mut tasks = Vec::new();
    for (index, core) in spec.cores.iter().cloned().enumerate() {
        let (tx, rx) = unbounded_channel();
        senders.push(tx);
        // A core's own directory: restarting one core never kills another core's executors.
        let executors = spec.state_dir.join("executors").join(&core.name);
        let supervised = Supervised {
            index,
            core,
            restart_max: spec.restart_max,
            restart_window: spec.restart_window,
            drain: spec.drain_timeout,
            sample: spec.sample,
            executors,
        };
        tasks.push(tokio::spawn(supervise(
            supervised,
            events.clone(),
            rx,
            fatal.clone(),
        )));
    }
    (senders, tasks)
}
