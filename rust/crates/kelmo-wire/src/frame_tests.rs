use serde_json::{Value, json};

use super::*;

fn header() -> Value {
    json!({"v": 2, "kind": "cancel", "id": "0".repeat(32), "body_type": "json"})
}

fn big_frame() -> Vec<u8> {
    let body = json!({"text": "x".repeat(3 << 20)});
    crate::encode(&header(), &body).expect("encodes")
}

#[test]
fn a_drained_large_buffer_gives_its_capacity_back() {
    let frame = big_frame();
    let mut reader = FrameReader::new();
    let (first, rest) = frame.split_at(frame.len() / 2);
    assert!(reader.feed(first).expect("partial").is_empty());
    assert!(reader.capacity() > SHRINK_ABOVE);
    assert_eq!(reader.feed(rest).expect("whole").len(), 1);
    assert_eq!(reader.pending(), 0);
    assert!(reader.capacity() <= SHRINK_ABOVE, "{}", reader.capacity());
}

#[test]
fn a_buffer_still_holding_a_large_partial_frame_keeps_it() {
    let frame = big_frame();
    let mut reader = FrameReader::new();
    let (first, _) = frame.split_at(frame.len() / 2);
    reader.feed(first).expect("partial");
    assert_eq!(reader.pending(), first.len());
    assert!(reader.capacity() >= first.len());
}

#[test]
fn a_small_buffer_keeps_its_capacity() {
    let frame = crate::encode(&header(), &json!({"n": 1})).expect("encodes");
    let mut reader = FrameReader::new();
    reader.feed(&frame).expect("whole");
    let before = reader.capacity();
    reader.feed(&frame).expect("whole");
    assert_eq!(reader.capacity(), before);
}
