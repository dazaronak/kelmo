//! The contract fixtures under tests/contract, read by this codec exactly as the Python codec reads them.

use std::path::PathBuf;

use kelmo_wire::{FrameReader, MAX_FRAME, decode, encode, identity_id};
use serde_json::{Value, json};

fn contract() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/contract")
}

fn manifest() -> Vec<Value> {
    let text = std::fs::read_to_string(contract().join("frames/manifest.json")).unwrap();
    serde_json::from_str::<Value>(&text)
        .unwrap()
        .as_array()
        .unwrap()
        .clone()
}

fn frame_bytes(entry: &Value) -> Vec<u8> {
    std::fs::read(
        contract()
            .join("frames")
            .join(entry["file"].as_str().unwrap()),
    )
    .unwrap()
}

#[test]
fn golden_frames_decode_encode_or_refuse_as_the_manifest_says() {
    let entries = manifest();
    assert!(entries.len() >= 30);
    for entry in entries {
        let name = entry["name"].as_str().unwrap();
        let data = frame_bytes(&entry);
        match entry.get("code") {
            Some(code) => {
                let error = decode(&data).expect_err(name);
                assert_eq!(error.code, code.as_str().unwrap(), "{name}: {error}");
            }
            None => {
                let (header, body) = decode(&data).unwrap_or_else(|e| panic!("{name}: {e}"));
                assert_eq!(header, entry["header"], "{name}");
                assert_eq!(body, entry["body"], "{name}");
                assert_eq!(
                    encode(&header, &body).unwrap(),
                    data,
                    "{name}: canonical bytes"
                );
            }
        }
    }
}

#[test]
fn a_stream_of_golden_frames_fed_byte_by_byte_yields_them_in_order() {
    let valid: Vec<Value> = manifest()
        .into_iter()
        .filter(|e| e.get("code").is_none())
        .collect();
    let stream: Vec<u8> = valid.iter().flat_map(frame_bytes).collect();
    let mut reader = FrameReader::new();
    let mut seen = Vec::new();
    for byte in stream {
        seen.extend(reader.feed(&[byte]).unwrap());
    }
    assert_eq!(reader.pending(), 0);
    assert_eq!(seen.len(), valid.len());
    for ((header, body), entry) in seen.iter().zip(&valid) {
        assert_eq!(header, &entry["header"]);
        assert_eq!(body, &entry["body"]);
    }
}

#[test]
fn a_frame_at_the_bound_passes_and_one_byte_more_is_refused() {
    let header = json!({"v": 2, "kind": "event", "id": "0123456789abcdef0123456789abcdef",
                        "op": "admin.status", "body_type": "json"});
    let head = kelmo_wire::canonical::to_string(&header).unwrap().len();
    let fill = MAX_FRAME - 2 - head - 2; // the body is a JSON string: two quotes
    let body = Value::String("a".repeat(fill));
    let frame = encode(&header, &body).unwrap();
    assert_eq!(frame.len(), 4 + MAX_FRAME);
    assert_eq!(decode(&frame).unwrap().1, body);
    let over = Value::String("a".repeat(fill + 1));
    assert_eq!(encode(&header, &over).unwrap_err().code, "E_LIMIT");
}

#[test]
fn identity_fixtures_give_the_same_canonical_text_and_id() {
    let text = std::fs::read_to_string(contract().join("identity.json")).unwrap();
    let cases: Vec<Value> = serde_json::from_str(&text).unwrap();
    assert_eq!(cases.len(), 2);
    for case in cases {
        let record = &case["record"];
        assert_eq!(
            kelmo_wire::canonical::to_string(record).unwrap(),
            case["canonical"].as_str().unwrap()
        );
        assert_eq!(identity_id(record).unwrap(), case["id"].as_str().unwrap());
    }
}

#[test]
fn the_line_codec_keeps_the_v1_refusals() {
    use kelmo_wire::line::{decode_line, encode_line};
    let value = json!({"op": "hello", "args": {}});
    let line = encode_line(&value).unwrap();
    assert_eq!(line, b"{\"args\":{},\"op\":\"hello\"}\n");
    assert_eq!(decode_line(&line).unwrap(), value);
    assert_eq!(decode_line(b"{\"op\":1}").unwrap_err().code, "E_PROTOCOL");
    assert_eq!(decode_line(b"[1]\n").unwrap_err().code, "E_PROTOCOL");
    let mut oversized = vec![b' '; MAX_FRAME];
    oversized.push(b'\n');
    assert_eq!(decode_line(&oversized).unwrap_err().code, "E_PROTOCOL");
}
