//! `kelmod embed`: the embedding core. It speaks kelmo/2 on descriptor 3 and answers
//!
//! - `embed.count {texts, prefix}`: token counts with the semantics of the Python `TokenWindow.count`;
//! - `embed.describe {}`: the tokenizer identity and the preparation digest the window was built with;
//! - `admin.ping`, `admin.status`, `admin.trim` (drops the tokenizer), `admin.stop`.
//!
//! The tokenizer loads at the first count and is dropped after `IDLE` without one. The tokenizer file and the
//! special-token additions come from the command line (`kelmo core-spec` reads them from the vault's
//! embedding settings), so a call cannot point the core at another file.

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use kelmo_embed::window::{Addition, Window};
use serde_json::{Value, json};

use crate::provider::{Config, Provider};

const IDLE: Duration = Duration::from_secs(300);
const OPERATIONS: &[&str] = &["embed.count", "embed.describe", "embed.embed"];

pub struct Options {
    pub tokenizer: PathBuf,
    pub additions: Vec<Addition>,
    pub preparation: String,
    pub provider: Option<Config>,
}

struct Loaded {
    window: Arc<Window>,
    used: Instant,
}

struct Core {
    options: Options,
    loaded: Mutex<Option<Loaded>>,
    writer: Mutex<UnixStream>,
    provider: Provider,
}

/// Parses the JSON list Python's `special_token_additions` validates: strings or AddedToken mappings.
pub fn additions(text: &str) -> Result<Vec<Addition>, String> {
    let value: Value = serde_json::from_str(text).map_err(|e| format!("--additions: {e}"))?;
    let items = value.as_array().ok_or("--additions: not a list")?;
    items
        .iter()
        .map(|item| match item {
            Value::String(content) if !content.is_empty() => Ok(Addition::plain(content)),
            Value::Object(fields) => {
                let content = fields
                    .get("content")
                    .and_then(Value::as_str)
                    .filter(|c| !c.is_empty())
                    .ok_or("--additions: a token without content")?;
                let flag = |name: &str| fields.get(name).and_then(Value::as_bool);
                Ok(Addition {
                    content: content.to_owned(),
                    single_word: flag("single_word").unwrap_or(false),
                    lstrip: flag("lstrip").unwrap_or(false),
                    rstrip: flag("rstrip").unwrap_or(false),
                    normalized: flag("normalized"),
                })
            }
            _ => Err("--additions: malformed token".to_owned()),
        })
        .collect()
}

pub(crate) fn error(code: &str, message: &str, retryable: bool) -> Value {
    json!({"code": code, "message": message, "retryable": retryable})
}

impl Core {
    fn send(&self, header: &Value, body: &Value) {
        match kelmo_wire::encode(header, body) {
            Ok(bytes) => {
                if let Ok(mut writer) = self.writer.lock() {
                    let _ = writer.write_all(&bytes);
                }
            }
            Err(error) => eprintln!("kelmod embed: a frame could not be encoded: {error}"),
        }
    }

    fn reply(&self, id: &str, result: Result<Value, Value>) {
        match result {
            Ok(body) => self.send(&kelmo_wire::header::reply(id, None), &body),
            Err(err) => self.send(&kelmo_wire::header::reply(id, Some(err)), &Value::Null),
        }
    }

    fn window(&self) -> Result<Arc<Window>, Value> {
        let mut loaded = self
            .loaded
            .lock()
            .map_err(|_| error("E_EMBEDDING", "window lock poisoned", false))?;
        if let Some(current) = loaded.as_mut() {
            current.used = Instant::now();
            return Ok(current.window.clone());
        }
        let data = std::fs::read(&self.options.tokenizer).map_err(|e| {
            error(
                "E_EMBEDDING",
                &format!(
                    "no tokenizer.json at {}: {e}",
                    self.options.tokenizer.display()
                ),
                false,
            )
        })?;
        let window = Arc::new(
            Window::from_bytes(&data, &self.options.additions).map_err(|e| {
                error(
                    "E_EMBEDDING",
                    &format!("{}: {}", e.reason, e.message),
                    false,
                )
            })?,
        );
        *loaded = Some(Loaded {
            window: window.clone(),
            used: Instant::now(),
        });
        Ok(window)
    }

    fn unload_idle(&self, force: bool) -> bool {
        let Ok(mut loaded) = self.loaded.lock() else {
            return false;
        };
        let idle = loaded
            .as_ref()
            .is_some_and(|current| force || current.used.elapsed() >= IDLE);
        if idle {
            *loaded = None;
        }
        idle
    }

    fn count(&self, body: &Value) -> Result<Value, Value> {
        let texts = body["texts"]
            .as_array()
            .ok_or_else(|| error("E_INPUT", "embed.count needs texts", false))?;
        let prefix = body["prefix"].as_str().unwrap_or("");
        let window = self.window()?;
        let mut counts = Vec::with_capacity(texts.len());
        for text in texts {
            let text = text
                .as_str()
                .ok_or_else(|| error("E_INPUT", "every text is a string", false))?;
            let count = window.count(prefix, text).map_err(|e| {
                error(
                    "E_EMBEDDING",
                    &format!("{}: {}", e.reason, e.message),
                    false,
                )
            })?;
            counts.push(count);
        }
        Ok(
            json!({"counts": counts, "identity": window.identity, "preparation": self.options.preparation}),
        )
    }

    fn describe(&self) -> Result<Value, Value> {
        let data = std::fs::read(&self.options.tokenizer)
            .map_err(|e| error("E_EMBEDDING", &format!("tokenizer unreadable: {e}"), false))?;
        use sha2::Digest;
        let identity = format!("sha256:{:x}", sha2::Sha256::digest(&data));
        Ok(json!({
            "tokenizer_identity": identity,
            "preparation": self.options.preparation,
            "provider": self.provider.configured(),
        }))
    }

    fn handle(self: &Arc<Self>, header: Value, body: Value) -> bool {
        let id = header["id"].as_str().unwrap_or("").to_owned();
        match header["op"].as_str().unwrap_or("") {
            "admin.ping" => self.reply(&id, Ok(json!({"pid": std::process::id()}))),
            "admin.status" => {
                let loaded = self.loaded.lock().map(|l| l.is_some()).unwrap_or(false);
                self.reply(
                    &id,
                    Ok(json!({
                        "pid": std::process::id(),
                        "tokenizer_loaded": loaded,
                        "provider": self.provider.status(),
                    })),
                );
            }
            "admin.trim" => {
                let tokenizer = self.unload_idle(true);
                let provider = self.provider.trim();
                self.reply(&id, Ok(json!({"trimmed": tokenizer || provider})));
            }
            "admin.stop" => {
                self.reply(&id, Ok(json!({"stopping": true})));
                return false;
            }
            "embed.count" => {
                let core = self.clone();
                std::thread::spawn(move || {
                    let result = core.count(&body);
                    core.reply(&id, result);
                });
            }
            "embed.embed" => {
                let core = self.clone();
                std::thread::spawn(move || {
                    let result = core.provider.call(&body);
                    core.reply(&id, result);
                });
            }
            "embed.describe" => self.reply(&id, self.describe()),
            other => self.reply(
                &id,
                Err(error(
                    "E_PROTOCOL",
                    &format!("{other} is not served here."),
                    false,
                )),
            ),
        }
        true
    }
}

/// Serves until kelmod closes the channel or sends admin.stop; returns the exit code.
pub fn serve(options: Options) -> i32 {
    let stream = match kelmo_runtime::sys::inherited_channel(3) {
        Ok(stream) => stream,
        Err(error) => {
            eprintln!("E_RUNTIME: descriptor 3 is not kelmod's channel: {error}");
            return 1;
        }
    };
    let Ok(writer) = stream.try_clone() else {
        return 1;
    };
    let provider = Provider::new(options.provider.clone());
    let core = Arc::new(Core {
        options,
        loaded: Mutex::new(None),
        writer: Mutex::new(writer),
        provider,
    });
    core.send(
        &json!({"v": 2, "kind": "hello", "id": format!("{:032x}", std::process::id()), "body_type": "json",
                "protocols": ["kelmo/2"], "operations": OPERATIONS}),
        &Value::Null,
    );
    let idle = core.clone();
    std::thread::spawn(move || {
        loop {
            std::thread::sleep(Duration::from_secs(10));
            idle.unload_idle(false);
        }
    });
    let mut reader = kelmo_wire::FrameReader::new();
    let mut stream = stream;
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        let read = match stream.read(&mut buffer) {
            Ok(0) | Err(_) => {
                core.provider.stop();
                return 0;
            }
            Ok(read) => read,
        };
        let Ok(frames) = reader.feed(&buffer[..read]) else {
            core.provider.stop();
            return 1;
        };
        for (header, body) in frames {
            if header["kind"] == "call" && !core.handle(header, body) {
                core.provider.stop();
                return 0;
            }
        }
    }
}
