//! Header validation, the same rules as `kelmo.core.wire.validate_header`.

use serde_json::{Map, Value};

use crate::{VERSION, WireError};

const KINDS: [&str; 5] = ["call", "reply", "cancel", "event", "hello"];

/// (field, kinds it may appear on, kinds that require it)
const FIELDS: [(&str, &[&str], &[&str]); 13] = [
    ("v", &KINDS, &KINDS),
    ("kind", &KINDS, &KINDS),
    ("id", &KINDS, &KINDS),
    ("op", &["call", "event"], &["call", "event"]),
    ("lane", &["call"], &[]),
    ("deadline_ms", &["call"], &["call"]),
    ("origin", &["call", "event", "hello"], &[]),
    ("key", &["call"], &[]),
    ("ok", &["reply"], &["reply"]),
    ("error", &["reply"], &[]),
    ("body_type", &KINDS, &KINDS),
    ("protocols", &["hello"], &["hello"]),
    ("operations", &["hello"], &["hello"]),
];

fn fail<T>(message: impl Into<String>) -> Result<T, WireError> {
    Err(WireError::protocol(message))
}

/// Checks a decoded header; refuses anything the Python codec refuses.
pub fn validate_header(header: &Value) -> Result<(), WireError> {
    let Value::Object(map) = header else {
        return fail("Frame header is not an object.");
    };
    let kind = match map.get("kind") {
        Some(Value::String(kind)) if KINDS.contains(&kind.as_str()) => kind.as_str(),
        _ => return fail("Frame kind is unknown."),
    };
    for name in map.keys() {
        match FIELDS.iter().find(|(field, _, _)| field == name) {
            Some((_, allowed, _)) if allowed.contains(&kind) => {}
            _ => {
                return fail(format!(
                    "Header field '{name}' is not part of a {kind} frame."
                ));
            }
        }
    }
    for (name, _, required) in FIELDS {
        if required.contains(&kind) && !map.contains_key(name) {
            return fail(format!("A {kind} frame requires header field '{name}'."));
        }
    }
    if map["v"].as_u64() != Some(VERSION) || !map["v"].is_u64() {
        return fail("Frame version is not 2.");
    }
    matches(&map["id"], "id", |s| {
        s.len() == 32
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })?;
    if map["body_type"] != Value::String("json".into()) {
        return fail("Frame body type is unknown.");
    }
    if let Some(op) = map.get("op") {
        matches(op, "op", valid_op)?;
    }
    if let Some(lane) = map.get("lane") {
        matches(lane, "lane", |s| {
            (1..=32).contains(&s.len()) && identifier(s)
        })?;
    }
    if let Some(deadline) = map.get("deadline_ms") {
        match deadline.as_u64() {
            Some(value) if deadline.is_u64() && value <= 86_400_000 => {}
            _ => return fail("deadline_ms is not an integer between 0 and 86400000."),
        }
    }
    if let Some(origin) = map.get("origin") {
        validate_origin(origin)?;
    }
    if let Some(key) = map.get("key") {
        matches(key, "key", |s| {
            (1..=240).contains(&s.len()) && s.bytes().all(|b| (0x20..=0x7e).contains(&b))
        })
        .map_err(|_| WireError::protocol("key is not 1 to 240 printable ASCII characters."))?;
    }
    if kind == "reply" {
        let Value::Bool(ok) = map["ok"] else {
            return fail("ok is not a boolean.");
        };
        if ok == map.contains_key("error") {
            return fail("A reply carries an error exactly when ok is false.");
        }
        if let Some(error) = map.get("error") {
            validate_error(error)?;
        }
    }
    if kind == "hello" {
        for name in ["protocols", "operations"] {
            let names = map[name].as_array().filter(|items| {
                items
                    .iter()
                    .all(|item| item.as_str().is_some_and(|s| !s.is_empty()))
            });
            if names.is_none() {
                return fail(format!("{name} is not a list of names."));
            }
        }
    }
    Ok(())
}

fn identifier(s: &str) -> bool {
    let mut bytes = s.bytes();
    bytes.next().is_some_and(|b| b.is_ascii_lowercase())
        && bytes.all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
}

fn valid_op(s: &str) -> bool {
    s.split_once('.')
        .is_some_and(|(core, name)| identifier(core) && identifier(name))
}

fn matches(value: &Value, name: &str, rule: impl Fn(&str) -> bool) -> Result<(), WireError> {
    match value.as_str() {
        Some(text) if rule(text) => Ok(()),
        _ => fail(format!("{name} is malformed.")),
    }
}

fn validate_origin(origin: &Value) -> Result<(), WireError> {
    let Value::Object(map) = origin else {
        return fail("origin is not {client_id, machine_tag, session}.");
    };
    let known = ["client_id", "machine_tag", "session"];
    if map.keys().any(|key| !known.contains(&key.as_str())) || !map.contains_key("client_id") {
        return fail("origin is not {client_id, machine_tag, session}.");
    }
    for (name, value) in map {
        let ok = match value {
            Value::Null => name != "client_id",
            Value::String(text) => (1..=200).contains(&text.chars().count()),
            _ => false,
        };
        if !ok {
            return fail(format!("origin.{name} is malformed."));
        }
    }
    Ok(())
}

fn validate_error(error: &Value) -> Result<(), WireError> {
    let Value::Object(map) = error else {
        return fail("error is not {code, message, retryable, retry_after_ms}.");
    };
    let known = ["code", "message", "retryable", "retry_after_ms"];
    let complete = ["code", "message", "retryable"]
        .iter()
        .all(|key| map.contains_key(*key));
    if !complete || map.keys().any(|key| !known.contains(&key.as_str())) {
        return fail("error is not {code, message, retryable, retry_after_ms}.");
    }
    matches(&map["code"], "error.code", |s| {
        s.strip_prefix("E_").is_some_and(|rest| {
            (1..=40).contains(&rest.len())
                && rest
                    .bytes()
                    .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_')
        })
    })?;
    if !map["message"].is_string() || !map["retryable"].is_boolean() {
        return fail("error message or retryable is malformed.");
    }
    if let Some(after) = map.get("retry_after_ms")
        && !after.is_null()
        && !after.is_u64()
    {
        return fail("error.retry_after_ms is not a non-negative integer.");
    }
    Ok(())
}

/// A reply header for `id`: ok, or carrying `error`.
pub fn reply(id: &str, error: Option<Value>) -> Value {
    let mut map = Map::new();
    map.insert("v".into(), VERSION.into());
    map.insert("kind".into(), "reply".into());
    map.insert("id".into(), id.into());
    map.insert("ok".into(), error.is_none().into());
    map.insert("body_type".into(), "json".into());
    if let Some(error) = error {
        map.insert("error".into(), error);
    }
    Value::Object(map)
}
