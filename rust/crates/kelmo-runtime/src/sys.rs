//! The only operating-system calls the safe standard library does not offer: moving a descriptor to a fixed
//! number in a child before exec, and reading another process's resident memory on macOS.
#![allow(unsafe_code)]

use std::io;
use std::os::fd::RawFd;

use tokio::process::Command;

/// Arranges for `fds[i].0` (open in this process, close-on-exec) to appear as descriptor `fds[i].1` in the
/// child, and for nothing else beyond stdio to be inherited.
pub fn map_descriptors(command: &mut Command, fds: Vec<(RawFd, RawFd)>) {
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
