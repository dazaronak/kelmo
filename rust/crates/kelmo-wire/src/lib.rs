//! Frame codec, header validation, error codes and canonical JSON of the `kelmo/2` protocol, and the line
//! codec of `kelmo-client/1` that `kelmod attach --protocol 1` keeps toward old clients.
//!
//! The frame is the same on every seam of the daemon:
//!
//! ```text
//! u32 BE  total length = 2 + header length + body length, at most MAX_FRAME
//! u16 BE  header length
//! header  UTF-8 JSON object
//! body    UTF-8 JSON value (body_type "json")
//! ```
//!
//! The Python codec (`kelmo.core.wire`) and this one read the same fixture files under `tests/contract`.

pub mod canonical;
pub mod frame;
pub mod header;
pub mod line;

use sha2::{Digest, Sha256};

pub use frame::{FrameReader, decode, encode};
pub use header::validate_header;

/// Upper bound of a whole frame, prefix excluded, and of a `/1` line, newline included.
pub const MAX_FRAME: usize = 32 * 1_048_576;
/// Upper bound of a frame header.
pub const MAX_HEADER: usize = 65_535;
/// The protocol version a `v` header field carries.
pub const VERSION: u64 = 2;

/// A refused frame or value: `E_PROTOCOL` for malformed input, `E_LIMIT` for input over a bound.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WireError {
    pub code: &'static str,
    pub message: String,
}

impl WireError {
    pub fn protocol(message: impl Into<String>) -> Self {
        Self {
            code: "E_PROTOCOL",
            message: message.into(),
        }
    }

    pub fn limit(message: impl Into<String>) -> Self {
        Self {
            code: "E_LIMIT",
            message: message.into(),
        }
    }
}

impl std::fmt::Display for WireError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for WireError {}

/// Error codes the daemon itself answers with. The first four are retryable by the caller's own rule.
pub const E_OVERLOADED: &str = "E_OVERLOADED";
pub const E_DEADLINE: &str = "E_DEADLINE";
pub const E_CORE_RESTARTED: &str = "E_CORE_RESTARTED";
pub const E_UNAVAILABLE: &str = "E_UNAVAILABLE";
pub const E_CANCELLED: &str = "E_CANCELLED";
pub const E_FORBIDDEN: &str = "E_FORBIDDEN";

/// `sha256:` plus the hex digest of a record's canonical JSON: the stored id of an `EmbeddingSpace` or a
/// `ChunkProfile`, as `kelmo.store.memory.embed.identity_id` computes it.
pub fn identity_id(record: &serde_json::Value) -> Result<String, WireError> {
    let text = canonical::to_string(record)?;
    let digest = Sha256::digest(text.as_bytes());
    let mut out = String::with_capacity(71);
    out.push_str("sha256:");
    for byte in digest {
        out.push_str(&format!("{byte:02x}"));
    }
    Ok(out)
}
