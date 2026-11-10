//! The `kelmo-client/1` line framing: one canonical JSON object per line, ended by `\n`, at most
//! `MAX_FRAME` bytes with the newline. The same rules and refusals as `encode_frame` and `decode_frame` in
//! `kelmo.server`.

use serde_json::Value;

use crate::{MAX_FRAME, WireError, canonical};

pub fn encode_line(value: &Value) -> Result<Vec<u8>, WireError> {
    let mut data = canonical::to_string(value)?.into_bytes();
    data.push(b'\n');
    if data.len() > MAX_FRAME {
        return Err(WireError::limit(
            "A request or reply exceeds the bounded frame size.",
        ));
    }
    Ok(data)
}

pub fn decode_line(line: &[u8]) -> Result<Value, WireError> {
    if line.len() > MAX_FRAME || !line.ends_with(b"\n") {
        return Err(WireError::protocol(
            "Frame is unterminated or exceeds the bounded frame size.",
        ));
    }
    let value: Value = std::str::from_utf8(line)
        .ok()
        .filter(|text| crate::frame::integers_fit(text))
        .and_then(|text| serde_json::from_str(text).ok())
        .ok_or_else(|| WireError::protocol("Frame is not one UTF-8 JSON object."))?;
    if !value.is_object() {
        return Err(WireError::protocol("Frame is not one JSON object."));
    }
    Ok(value)
}
