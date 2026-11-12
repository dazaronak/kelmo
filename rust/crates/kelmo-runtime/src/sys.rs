//! The only operating-system calls the safe standard library does not offer: moving a descriptor to a fixed
//! number in a child before exec, and reading another process's resident memory on macOS.
#![allow(unsafe_code)]

use std::io;
use std::os::fd::RawFd;
use std::sync::atomic::{AtomicU64, Ordering};

use tokio::process::Command;

use crate::spec::MemoryScope;

/// The open-file soft limit a core is started with, once `raise_open_files` raised kelmod's own (0 before).
/// Python code that uses select(2) fails on a descriptor past 1024, which a core under kelmod's raised limit
/// could be handed; a service manager's own soft limit (launchd 256) is too low for a core with executor
/// children. So a core gets `SELECT_LIMIT`, or what kelmod was started with when that was higher.
static CORE_OPEN_FILES: AtomicU64 = AtomicU64::new(0);

/// `FD_SETSIZE`: the first descriptor number select(2) cannot watch.
const SELECT_LIMIT: u64 = 1024;

/// Highest descriptor number the child sweep looks at; a soft limit of "unlimited" would make the sweep
/// endless, and a daemon that holds more descriptors than this has a different problem.
const SWEEP_LIMIT: RawFd = 1 << 16;

/// Arranges for `fds[i].0` (open in this process, close-on-exec) to appear as descriptor `fds[i].1` in the
/// child, and for nothing else beyond stdio to be inherited.
///
/// Where the platform has no atomic close-on-exec (macOS: `pipe` and `socketpair` set the flag in a second
/// call), a descriptor another thread created a moment before the fork reaches the child without the flag.
/// A long-lived core holding the write end of another spawn's exec-status pipe blocks that spawn forever, and
/// cores holding each other's channels outlive the daemon. So the child marks every descriptor from 3 up,
/// except the mapped targets, close-on-exec before it execs.
pub fn map_descriptors(command: &mut Command, fds: Vec<(RawFd, RawFd)>) {
    // SAFETY: getdtablesize has no memory effects.
    let limit = unsafe { libc::getdtablesize() }.clamp(4, SWEEP_LIMIT);
    let core_open_files = CORE_OPEN_FILES.load(Ordering::Relaxed);
    // SAFETY: the closure runs in the forked child before exec and allocates nothing. dup2 and fcntl are
    // async-signal-safe; getrlimit and setrlimit are not on POSIX's list, but on Linux and macOS they are plain
    // system calls that take no lock and touch only the struct passed to them.
    unsafe {
        command.pre_exec(move || {
            for &(source, target) in &fds {
                if source == target {
                    let flags = libc::fcntl(source, libc::F_GETFD);
                    if flags < 0
                        || libc::fcntl(source, libc::F_SETFD, flags & !libc::FD_CLOEXEC) < 0
                    {
                        return Err(io::Error::last_os_error());
                    }
                } else if libc::dup2(source, target) < 0 {
                    return Err(io::Error::last_os_error());
                }
            }
            for fd in 3..limit {
                if fds.iter().any(|&(_, target)| target == fd) {
                    continue;
                }
                let flags = libc::fcntl(fd, libc::F_GETFD);
                if flags >= 0 && flags & libc::FD_CLOEXEC == 0 {
                    libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC);
                }
            }
            if core_open_files > 0 {
                let mut open_files = libc::rlimit {
                    rlim_cur: 0,
                    rlim_max: 0,
                };
                if libc::getrlimit(libc::RLIMIT_NOFILE, &mut open_files) != 0 {
                    return Err(io::Error::last_os_error());
                }
                open_files.rlim_cur = core_open_files;
                // A core left under kelmod's limit would fail later, in select(2), with nothing naming why.
                if libc::setrlimit(libc::RLIMIT_NOFILE, &open_files) != 0 {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(())
        });
    }
}

/// Puts the child in its own process group, so a kill of the group reaches what it started.
pub fn new_process_group(command: &mut Command) {
    command.process_group(0);
}

/// Sends SIGKILL to a process group.
pub fn kill_group(pid: u32) {
    // SAFETY: kill has no memory effects; a stale group id fails with ESRCH, which is ignored.
    unsafe {
        libc::kill(-(pid as libc::pid_t), libc::SIGKILL);
    }
}

/// Resident memory of a process in bytes, if it can be read.
#[cfg(target_os = "linux")]
pub fn resident_bytes(pid: u32) -> Option<u64> {
    let status = std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    let line = status.lines().find(|line| line.starts_with("VmRSS:"))?;
    let kib: u64 = line.split_whitespace().nth(1)?.parse().ok()?;
    Some(kib * 1024)
}

#[cfg(target_os = "macos")]
pub fn resident_bytes(pid: u32) -> Option<u64> {
    let mut info: libc::proc_taskinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_taskinfo>() as libc::c_int;
    // SAFETY: proc_pidinfo writes at most `size` bytes into `info`, a properly sized and aligned struct.
    let written = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDTASKINFO,
            0,
            (&mut info as *mut libc::proc_taskinfo).cast(),
            size,
        )
    };
    (written == size).then_some(info.pti_resident_size)
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub fn resident_bytes(_pid: u32) -> Option<u64> {
    None
}

/// True when a process with this pid exists (it may belong to another user).
pub fn alive(pid: i32) -> bool {
    // SAFETY: signal 0 only checks that the process exists; no memory effects.
    let result = unsafe { libc::kill(pid, 0) };
    result == 0 || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

/// The real uid of this process.
pub fn uid() -> u32 {
    // SAFETY: getuid cannot fail and has no memory effects.
    unsafe { libc::getuid() }
}

/// Raises the soft limit on open descriptors toward the hard limit, to at most `cap`, and returns the soft limit
/// in force afterwards. Service managers start daemons low (launchd 256, systemd 1024), and every HTTP connection
/// and core channel holds a descriptor. macOS refuses a soft limit above `OPEN_MAX` (10240), so callers cap there.
pub fn raise_open_files(cap: u64) -> u64 {
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    // SAFETY: getrlimit and setrlimit only read and write the struct passed to them.
    if unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &mut limit) } != 0 {
        return 0;
    }
    // `rlim_t` is u64 on every target kelmod builds for (macOS, 64-bit Linux).
    let wanted: libc::rlim_t = limit.rlim_max.min(cap);
    if wanted > limit.rlim_cur {
        let raised = libc::rlimit {
            rlim_cur: wanted,
            rlim_max: limit.rlim_max,
        };
        if unsafe { libc::setrlimit(libc::RLIMIT_NOFILE, &raised) } == 0 {
            CORE_OPEN_FILES.store(
                limit.rlim_cur.max(wanted.min(SELECT_LIMIT)),
                Ordering::Relaxed,
            );
            return wanted;
        }
    }
    limit.rlim_cur
}

/// This process's resident memory.
pub fn own_resident_bytes() -> Option<u64> {
    resident_bytes(std::process::id())
}

/// The channel kelmod handed a core on descriptor 3, as a stream. Called once, at the core's start.
pub fn inherited_channel(fd: RawFd) -> io::Result<std::os::unix::net::UnixStream> {
    use std::os::fd::FromRawFd;
    // SAFETY: `fd` is descriptor 3, which kelmod opened for this process and nothing else in it owns; it is
    // taken over exactly once, here. fstat confirms it is open before ownership is claimed.
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    if unsafe { libc::fstat(fd, &mut stat) } != 0 {
        return Err(io::Error::last_os_error());
    }
    // A program the core starts (the embedding provider) must not inherit the channel to kelmod.
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(unsafe { std::os::unix::net::UnixStream::from_raw_fd(fd) })
}

/// The direct children of a process.
#[cfg(target_os = "linux")]
fn children(pid: u32) -> Vec<u32> {
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter_map(|entry| entry.file_name().to_str()?.parse::<u32>().ok())
        .filter(|child| {
            std::fs::read_to_string(format!("/proc/{child}/stat"))
                .ok()
                .and_then(|stat| {
                    // The command name in parentheses may hold spaces: the fields after its closing ')'.
                    let rest = &stat[stat.rfind(')')? + 1..];
                    rest.split_whitespace().nth(1)?.parse::<u32>().ok()
                })
                == Some(pid)
        })
        .collect()
}

#[cfg(target_os = "macos")]
fn children(pid: u32) -> Vec<u32> {
    let mut buffer = vec![0 as libc::pid_t; 256];
    let bytes = (buffer.len() * std::mem::size_of::<libc::pid_t>()) as libc::c_int;
    // SAFETY: proc_listchildpids writes at most `bytes` bytes of pids into `buffer` and returns how many it wrote.
    let count =
        unsafe { libc::proc_listchildpids(pid as libc::pid_t, buffer.as_mut_ptr().cast(), bytes) };
    if count <= 0 {
        return Vec::new();
    }
    buffer
        .into_iter()
        .take(count as usize)
        .filter(|child| *child > 0)
        .map(|child| child as u32)
        .collect()
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn children(_pid: u32) -> Vec<u32> {
    Vec::new()
}

/// The process group of a process.
#[cfg(target_os = "linux")]
fn process_group(pid: u32) -> Option<u32> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    // Field 5; the command name in parentheses may hold spaces, so count from its closing ')'.
    let rest = &stat[stat.rfind(')')? + 1..];
    rest.split_whitespace().nth(2)?.parse().ok()
}

#[cfg(target_os = "macos")]
fn process_group(pid: u32) -> Option<u32> {
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    // SAFETY: proc_pidinfo writes at most `size` bytes into `info` and returns how many it wrote.
    let written = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDTBSDINFO,
            0,
            (&mut info as *mut libc::proc_bsdinfo).cast(),
            size,
        )
    };
    (written == size).then_some(info.pbi_pgid)
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn process_group(_pid: u32) -> Option<u32> {
    None
}

/// The descendants of `leader` a budget of this scope counts: every one for `Tree`, those in the leader's
/// process group for `Group`. Every descendant is walked (a group member may sit below a process of another
/// group), at most 4096 of them.
fn counted_descendants(
    leader: u32,
    scope: MemoryScope,
    children: impl Fn(u32) -> Vec<u32>,
    group: impl Fn(u32) -> Option<u32>,
) -> Vec<u32> {
    let mut members = Vec::new();
    let mut pending = children(leader);
    let mut seen = 0;
    while let Some(child) = pending.pop() {
        seen += 1;
        if seen > 4096 {
            break;
        }
        if scope == MemoryScope::Tree || group(child) == Some(leader) {
            members.push(child);
        }
        pending.extend(children(child));
    }
    members
}

/// Resident memory of a core, which leads its own process group (kelmod kills it with `kill_group`), and of
/// the descendants its budget scope counts (see `MemoryScope`).
pub fn core_resident_bytes(pid: u32, scope: MemoryScope) -> Option<u64> {
    let mut total = resident_bytes(pid)?;
    for member in counted_descendants(pid, scope, children, process_group) {
        total += resident_bytes(member).unwrap_or(0);
    }
    Some(total)
}

/// When a process started, as the opaque string `kelmo.daemon.process_start` derives the same way, or None
/// when it is gone. Linux: field 22 of `/proc/<pid>/stat`; macOS: `pbi_start_tvsec.pbi_start_tvusec`.
#[cfg(target_os = "linux")]
pub fn process_start(pid: u32) -> Option<String> {
    let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
    let rest = stat.rsplit_once(')')?.1;
    rest.split_whitespace().nth(19).map(str::to_owned)
}

#[cfg(target_os = "macos")]
pub fn process_start(pid: u32) -> Option<String> {
    let mut info: libc::proc_bsdinfo = unsafe { std::mem::zeroed() };
    let size = std::mem::size_of::<libc::proc_bsdinfo>() as libc::c_int;
    // SAFETY: proc_pidinfo writes at most `size` bytes into `info` and returns how many it wrote.
    let written = unsafe {
        libc::proc_pidinfo(
            pid as libc::c_int,
            libc::PROC_PIDTBSDINFO,
            0,
            (&mut info as *mut libc::proc_bsdinfo).cast(),
            size,
        )
    };
    if written != size {
        return None;
    }
    Some(format!(
        "{}.{:06}",
        info.pbi_start_tvsec, info.pbi_start_tvusec
    ))
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub fn process_start(_pid: u32) -> Option<String> {
    None
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::*;

    /// (pid, parent, group) of a synthetic process tree.
    fn tree(rows: &[(u32, u32, u32)]) -> Vec<u32> {
        let parents: HashMap<u32, u32> = rows.iter().map(|(p, parent, _)| (*p, *parent)).collect();
        let groups: HashMap<u32, u32> = rows.iter().map(|(p, _, g)| (*p, *g)).collect();
        let mut members = counted_descendants(
            100,
            MemoryScope::Group,
            |pid| {
                let mut kids: Vec<u32> = parents
                    .iter()
                    .filter(|(_, parent)| **parent == pid)
                    .map(|(child, _)| *child)
                    .collect();
                kids.sort_unstable();
                kids
            },
            |pid| groups.get(&pid).copied(),
        );
        members.sort_unstable();
        members
    }

    #[test]
    fn children_in_the_cores_group_count() {
        assert_eq!(tree(&[(101, 100, 100), (102, 101, 100)]), vec![101, 102]);
    }

    #[test]
    fn a_child_in_a_session_of_its_own_and_its_children_do_not_count() {
        assert_eq!(
            tree(&[(101, 100, 100), (200, 100, 200), (201, 200, 200)]),
            vec![101]
        );
    }

    #[test]
    fn a_group_member_below_a_foreign_process_still_counts() {
        assert_eq!(tree(&[(200, 100, 200), (201, 200, 100)]), vec![201]);
    }

    #[test]
    fn a_process_whose_group_cannot_be_read_does_not_count() {
        let children = |pid: u32| if pid == 100 { vec![7] } else { Vec::new() };
        assert!(counted_descendants(100, MemoryScope::Group, children, |_| None).is_empty());
    }

    #[test]
    fn a_tree_budget_counts_a_child_in_a_session_of_its_own() {
        // The confined archive worker: bubblewrap stays in the core's group, the worker it starts does not.
        let parents = HashMap::from([(101, 100), (200, 101)]);
        let groups = HashMap::from([(101, 100), (200, 200)]);
        let children = |pid: u32| {
            let mut kids: Vec<u32> = parents
                .iter()
                .filter(|(_, p)| **p == pid)
                .map(|(c, _)| *c)
                .collect();
            kids.sort_unstable();
            kids
        };
        let mut members = counted_descendants(100, MemoryScope::Tree, children, |pid| {
            groups.get(&pid).copied()
        });
        members.sort_unstable();
        assert_eq!(members, vec![101, 200]);
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    #[test]
    fn the_group_of_this_process_is_read() {
        // SAFETY: getpgrp cannot fail and has no memory effects.
        let own = unsafe { libc::getpgrp() } as u32;
        assert_eq!(process_group(std::process::id()), Some(own));
    }
}
