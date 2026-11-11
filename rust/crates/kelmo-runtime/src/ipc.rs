//! Local IPC: the instance lock that makes this daemon the root's only owner, the short runtime directory
//! that holds the protocol socket, and the listener that admits only peers of this daemon's own uid.

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};
use tokio::net::{UnixListener, UnixStream};

/// The first 8 hex characters of sha256 over the root's real path: the runtime directory name of that root.
pub fn root_id(root: &Path) -> std::io::Result<String> {
    let real = fs::canonicalize(root)?;
    let digest = Sha256::digest(real.as_os_str().as_encoded_bytes());
    Ok(digest[..4].iter().map(|b| format!("{b:02x}")).collect())
}

/// `$XDG_RUNTIME_DIR/kelmo/<id8>` on Linux, `$TMPDIR/kelmo/<id8>` elsewhere. A socket path is limited to
/// about 104 bytes, so the socket never lives under the vault.
pub fn runtime_dir(root: &Path) -> std::io::Result<PathBuf> {
    let base = std::env::var_os("XDG_RUNTIME_DIR")
        .filter(|value| cfg!(target_os = "linux") && !value.is_empty())
        .or_else(|| std::env::var_os("TMPDIR").filter(|value| !value.is_empty()))
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp"));
    Ok(base.join("kelmo").join(root_id(root)?))
}

/// Refuses a runtime directory another user could have planted or can write into: it must be a real
/// directory (not a symlink), owned by this process's uid, with no group or other permission bits.
pub fn trusted_directory(directory: &Path) -> Result<(), String> {
    use std::os::unix::fs::MetadataExt;
    let metadata = fs::symlink_metadata(directory)
        .map_err(|e| format!("{}: {e}; is the daemon running?", directory.display()))?;
    if !metadata.file_type().is_dir() {
        return Err(format!("{} is not a directory", directory.display()));
    }
    if metadata.uid() != crate::sys::uid() {
        return Err(format!("{} belongs to another user", directory.display()));
    }
    if metadata.mode() & 0o077 != 0 {
        return Err(format!("{} is open to other users", directory.display()));
    }
    Ok(())
}

pub fn socket_path(runtime: &Path) -> PathBuf {
    runtime.join("d.sock")
}

/// Held for the life of the daemon. A second daemon on the same root fails to take it.
pub struct InstanceLock {
    _file: File,
}

impl InstanceLock {
    pub fn acquire(path: &Path) -> Result<Self, String> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).map_err(|e| format!("{}: {e}", parent.display()))?;
        }
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(path)
            .map_err(|e| format!("{}: {e}", path.display()))?;
        match file.try_lock() {
            Ok(()) => Ok(InstanceLock { _file: file }),
            Err(std::fs::TryLockError::WouldBlock) => {
                Err(format!("{} is held by another daemon", path.display()))
            }
            Err(std::fs::TryLockError::Error(error)) => Err(format!("{}: {error}", path.display())),
        }
    }
}

/// Creates the runtime directory (mode 0700), removes a stale socket, binds a new one and writes `pid`.
/// Called only while the instance lock is held, so a socket found here is a dead daemon's.
pub fn bind(runtime: &Path) -> std::io::Result<UnixListener> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(runtime)?;
    fs::set_permissions(runtime, fs::Permissions::from_mode(0o700))?;
    trusted_directory(runtime).map_err(std::io::Error::other)?;
    let socket = socket_path(runtime);
    match fs::remove_file(&socket) {
        Err(error) if error.kind() != std::io::ErrorKind::NotFound => return Err(error),
        _ => {}
    }
    let listener = UnixListener::bind(&socket)?;
    let mut pid = File::create(runtime.join("pid"))?;
    writeln!(pid, "{}", std::process::id())?;
    Ok(listener)
}

/// True when the peer runs under this process's uid.
pub fn same_user(stream: &UnixStream) -> bool {
    match stream.peer_cred() {
        Ok(credentials) => credentials.uid() == crate::sys::uid(),
        Err(_) => false,
    }
}
