//! The only operating-system calls the safe standard library does not offer: moving a descriptor to a fixed
//! number in a child before exec, and reading another process's resident memory on macOS.
#![allow(unsafe_code)]

use std::io;
use std::os::fd::RawFd;

use tokio::process::Command;

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
    // SAFETY: the closure runs in the forked child before exec and calls only dup2 and fcntl, which are
    // async-signal-safe; it allocates nothing.
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

/// The real uid of this process.
pub fn uid() -> u32 {
    // SAFETY: getuid cannot fail and has no memory effects.
    unsafe { libc::getuid() }
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

/// Resident memory of a process and every process below it: a core's launcher and the confined worker it
/// starts (bubblewrap puts that worker in a session of its own, so its process group is not the core's).
pub fn tree_resident_bytes(pid: u32) -> Option<u64> {
    let mut total = resident_bytes(pid)?;
    let mut pending = children(pid);
    let mut seen = 0;
    while let Some(child) = pending.pop() {
        seen += 1;
        if seen > 4096 {
            break;
        }
        total += resident_bytes(child).unwrap_or(0);
        pending.extend(children(child));
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
