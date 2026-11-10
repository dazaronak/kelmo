//! The `kelmo/2` frame codec.

use serde_json::Value;

use crate::{MAX_FRAME, MAX_HEADER, WireError, canonical, header::validate_header};

/// Bytes of one frame. The header is validated before anything is written.
pub fn encode(header: &Value, body: &Value) -> Result<Vec<u8>, WireError> {
    validate_header(header)?;
    let head = canonical::to_string(header)?;
    let data = canonical::to_string(body)?;
    if head.len() > MAX_HEADER {
        return Err(WireError::limit("Frame header exceeds 65535 bytes."));
    }
    let total = 2 + head.len() + data.len();
    if total > MAX_FRAME {
        return Err(WireError::limit("Frame exceeds the bounded frame size."));
    }
    let mut out = Vec::with_capacity(4 + total);
    out.extend_from_slice(&(total as u32).to_be_bytes());
    out.extend_from_slice(&(head.len() as u16).to_be_bytes());
    out.extend_from_slice(head.as_bytes());
    out.extend_from_slice(data.as_bytes());
    Ok(out)
}

/// (header, body) of one complete frame, prefix included.
pub fn decode(frame: &[u8]) -> Result<(Value, Value), WireError> {
    if frame.len() < 4 {
        return Err(WireError::protocol(
            "Frame is shorter than its length prefix.",
        ));
    }
    let total = u32::from_be_bytes([frame[0], frame[1], frame[2], frame[3]]) as usize;
    if total > MAX_FRAME {
        return Err(WireError::limit("Frame exceeds the bounded frame size."));
    }
    if frame.len() != 4 + total {
        return Err(WireError::protocol(
            "Frame length does not match its prefix.",
        ));
    }
    decode_payload(&frame[4..])
}

fn decode_payload(payload: &[u8]) -> Result<(Value, Value), WireError> {
    if payload.len() < 2 {
        return Err(WireError::protocol("Frame has no header length."));
    }
    let head_length = u16::from_be_bytes([payload[0], payload[1]]) as usize;
    if head_length == 0 || 2 + head_length > payload.len() {
        return Err(WireError::protocol(
            "Frame header length is outside the frame.",
        ));
    }
    let header = json(&payload[2..2 + head_length], "header")?;
    if !header.is_object() {
        return Err(WireError::protocol("Frame header is not a JSON object."));
    }
    validate_header(&header)?;
    let body = json(&payload[2 + head_length..], "body")?;
    Ok((header, body))
}

fn json(data: &[u8], part: &str) -> Result<Value, WireError> {
    let refused = || WireError::protocol(format!("Frame {part} is not UTF-8 JSON."));
    let text = std::str::from_utf8(data).map_err(|_| refused())?;
    if !integers_fit(text) {
        return Err(refused());
    }
    serde_json::from_str(text).map_err(|_| refused())
}

/// False when the text holds an integer literal outside i64 and u64. serde_json would read it as a float
/// and the canonical bytes would change; the Python codec refuses it, so this one does too.
pub(crate) fn integers_fit(text: &str) -> bool {
    let bytes = text.as_bytes();
    let (mut index, mut in_string, mut escaped) = (0, false, false);
    while index < bytes.len() {
        let byte = bytes[index];
        if in_string {
            match (escaped, byte) {
                (true, _) => escaped = false,
                (false, b'\\') => escaped = true,
                (false, b'"') => in_string = false,
                _ => {}
            }
            index += 1;
            continue;
        }
        if byte == b'"' {
            in_string = true;
            index += 1;
        } else if byte == b'-' || byte.is_ascii_digit() {
            let start = index;
            while index < bytes.len()
                && matches!(bytes[index], b'-' | b'+' | b'.' | b'e' | b'E' | b'0'..=b'9')
            {
                index += 1;
            }
            let token = &text[start..index];
            let integral = !token.contains(['.', 'e', 'E']);
            if integral && token.parse::<i64>().is_err() && token.parse::<u64>().is_err() {
                return false;
            }
        } else {
            index += 1;
        }
    }
    true
}

/// Incremental decoding of a byte stream into frames. Feed what arrives; take what is complete.
///
/// A prefix that announces a frame over the bound is refused at once, before its bytes are buffered.
#[derive(Default)]
pub struct FrameReader {
    buffer: Vec<u8>,
}

impl FrameReader {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn feed(&mut self, data: &[u8]) -> Result<Vec<(Value, Value)>, WireError> {
        self.buffer.extend_from_slice(data);
        let mut frames = Vec::new();
        let mut start = 0;
        while self.buffer.len() - start >= 4 {
            let prefix = &self.buffer[start..start + 4];
            let total = u32::from_be_bytes([prefix[0], prefix[1], prefix[2], prefix[3]]) as usize;
            if total > MAX_FRAME {
                return Err(WireError::limit("Frame exceeds the bounded frame size."));
            }
            if self.buffer.len() - start < 4 + total {
                break;
            }
            frames.push(decode_payload(&self.buffer[start + 4..start + 4 + total])?);
            start += 4 + total;
        }
        self.buffer.drain(..start);
        Ok(frames)
    }

    /// Bytes received but not yet part of a complete frame.
    pub fn pending(&self) -> usize {
        self.buffer.len()
    }
}
