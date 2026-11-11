//! Descriptors a child spawned through `map_descriptors` must not inherit, and the mapped one it must keep.
#![cfg(unix)]
#![allow(unsafe_code)]

use std::io::Read;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::net::UnixStream;
use std::process::Stdio;
use std::time::Duration;

use kelmo_runtime::sys::map_descriptors;
use tokio::process::Command;

/// A pipe whose ends lack close-on-exec, the state a descriptor from a sibling thread is in before its flag
/// is set. The ends are copied above 10 so they never collide with descriptor 3, the one the tests map onto.
fn pipe_without_cloexec() -> (OwnedFd, OwnedFd) {
    let mut fds = [0; 2];
    // SAFETY: pipe writes two descriptors into the array.
    assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
    // SAFETY: both descriptors were just returned by pipe and are owned only here.
    let ends = unsafe { [OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])] };
    let [read, write] = ends.map(|end| {
        // SAFETY: F_DUPFD only reads `end`; the descriptor it returns is owned by nothing else.
        let copy = unsafe { libc::fcntl(end.as_raw_fd(), libc::F_DUPFD, 10) };
        assert!(copy >= 0);
        // SAFETY: `copy` was just returned by fcntl and is taken over here, once.
        unsafe { OwnedFd::from_raw_fd(copy) }
    });
    (read, write)
}

/// Only the mapped descriptor 3 is open in the child; the leaked pipe ends are closed by the sweep.
#[tokio::test]
async fn unmapped_descriptors_are_closed_in_child() {
    let (read, write) = pipe_without_cloexec();
    let (_parent_end, child_end) = UnixStream::pair().unwrap();
    let (read_fd, write_fd) = (read.as_raw_fd(), write.as_raw_fd());
    let script = format!(
        "for fd in 3 {read_fd} {write_fd}; do \
         if (: >&$fd) 2>/dev/null; then echo \"$fd=yes\"; else echo \"$fd=no\"; fi; done"
    );
    let mut command = Command::new("/bin/sh");
    command.arg("-c").arg(script);
    map_descriptors(&mut command, vec![(child_end.as_raw_fd(), 3)]);
    let output = command.output().await.unwrap();
    let expected = format!("3=yes\n{read_fd}=no\n{write_fd}=no\n");
    assert_eq!(output.stdout, expected.as_bytes());
}

/// A child spawned while another pipe's write end is open must not keep that write end, so the reader sees EOF.
#[tokio::test]
async fn child_does_not_hold_another_pipe_write_end() {
    let (read, write) = pipe_without_cloexec();
    let mut command = Command::new("/bin/sleep");
    command
        .arg("5")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    map_descriptors(&mut command, Vec::new());
    let mut child = command.spawn().unwrap();
    drop(write);
    let mut reader = std::fs::File::from(read);
    let eof = tokio::time::timeout(
        Duration::from_secs(2),
        tokio::task::spawn_blocking(move || reader.read_to_end(&mut Vec::new()).is_ok()),
    )
    .await;
    child.kill().await.unwrap();
    assert!(
        matches!(eof, Ok(Ok(true))),
        "pipe did not reach EOF within 2 s"
    );
}
