//! The Kelmo daemon binary.
//!
//! ```text
//! kelmod --version
//! kelmod serve --spec FILE [--ephemeral]
//! kelmod embed --tokenizer FILE [--additions JSON] [--preparation DIGEST]
//!              [--provider-command JSON_ARGV] [--provider-env JSON_OBJECT]
//! kelmod status (--root DIR | --runtime-dir DIR)
//! kelmod call OP (--root DIR | --runtime-dir DIR) [--body JSON] [--lane L] [--deadline-ms N] [--key K]
//! kelmod attach --client-id ID [--protocol 1|2|auto] (default 1) (--root DIR | --runtime-dir DIR)
//! kelmod mcp-stdio (--root DIR | --runtime-dir DIR)
//! kelmod hook ACTION [--provider P] [--state DIR] (--root DIR | --runtime-dir DIR)
//! kelmod http-headers --state DIR --port N (--root DIR | --runtime-dir DIR)
//! ```
//! `status` and `call` print one JSON object: the reply body on success, `{"error": ...}` on a refusal.

use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Duration;

use kelmo_runtime::{client, daemon, ipc, spec::Spec};

mod attach;
mod embed;
mod hook;
mod http;
mod provider;
use serde_json::{Value, json};

/// Exit code for a spec the daemon cannot serve (sysexits EX_CONFIG).
const EXIT_CONFIG: i32 = daemon::EXIT_CONFIG;

/// The package version in the form Python packaging uses, so `kelmod --version` and
/// `kelmo --version` print the same string.
fn package_version() -> String {
    pep440(env!("CARGO_PKG_VERSION"))
}

/// Converts a Cargo version such as `0.2.0-dev.0` into its PEP 440 spelling `0.2.0.dev0`.
fn pep440(version: &str) -> String {
    let Some((release, pre)) = version.split_once('-') else {
        return version.to_owned();
    };
    let (label, number) = pre.split_once('.').unwrap_or((pre, "0"));
    let label = match label {
        "alpha" => "a",
        "beta" => "b",
        "rc" => "rc",
        "dev" => ".dev",
        other => other,
    };
    format!("{release}{label}{number}")
}

const USAGE: &str = "usage: kelmod --version | serve --spec FILE [--ephemeral] | embed --tokenizer FILE [--additions JSON] [--preparation DIGEST] [--provider-command JSON_ARGV] [--provider-env JSON_OBJECT] | status (--root DIR | --runtime-dir DIR) \
| call OP (--root DIR | --runtime-dir DIR) [--body JSON] [--lane L] [--deadline-ms N] [--key K] \
| attach --client-id ID [--protocol 1|2|auto] (default 1) (--root DIR | --runtime-dir DIR) | mcp-stdio (--root DIR | --runtime-dir DIR) \
| hook ACTION [--provider P] [--state DIR] (--root DIR | --runtime-dir DIR) \
| http-headers --state DIR --port N (--root DIR | --runtime-dir DIR)";

/// Options that take no value.
const FLAGS: &[&str] = &["ephemeral"];

struct Options {
    positional: Vec<String>,
    named: std::collections::HashMap<String, String>,
}

fn parse(args: &[String]) -> Result<Options, String> {
    let mut positional = Vec::new();
    let mut named = std::collections::HashMap::new();
    let mut index = 0;
    while index < args.len() {
        let arg = &args[index];
        if let Some(name) = arg.strip_prefix("--") {
            if FLAGS.contains(&name) {
                named.insert(name.to_owned(), String::new());
                index += 1;
                continue;
            }
            let value = args
                .get(index + 1)
                .ok_or_else(|| format!("--{name} needs a value"))?;
            named.insert(name.to_owned(), value.clone());
            index += 2;
        } else {
            positional.push(arg.clone());
            index += 1;
        }
    }
    Ok(Options { positional, named })
}

fn runtime_dir(options: &Options) -> Result<PathBuf, String> {
    match (options.named.get("runtime-dir"), options.named.get("root")) {
        (Some(dir), _) => Ok(PathBuf::from(dir)),
        (None, Some(root)) => ipc::runtime_dir(&PathBuf::from(root)).map_err(|e| e.to_string()),
        (None, None) => Err("name --root or --runtime-dir".into()),
    }
}

fn socket(options: &Options) -> Result<PathBuf, String> {
    Ok(ipc::socket_path(&runtime_dir(options)?))
}

fn print_reply(result: Result<(Value, Value), String>) -> ExitCode {
    match result {
        Ok((header, body)) if header["ok"] == Value::Bool(true) => {
            println!("{body}");
            ExitCode::SUCCESS
        }
        Ok((header, _)) => {
            println!("{}", json!({"error": header["error"]}));
            ExitCode::from(1)
        }
        Err(message) => {
            let (code, text) = message
                .split_once(": ")
                .unwrap_or(("E_TRANSPORT", &message));
            println!(
                "{}",
                json!({"error": {"code": code, "message": text, "retryable": true}})
            );
            ExitCode::from(1)
        }
    }
}

/// Runs a bridge (attach, mcp-stdio, hook) to its end. Its runtime is shut down without joining the blocking
/// threads, so a stdin read still parked there cannot keep the process alive after the bridge is done.
fn bridge<F: std::future::Future<Output = i32>>(future: F) -> ExitCode {
    let runtime = runtime();
    let code = runtime.block_on(future);
    runtime.shutdown_background();
    ExitCode::from(code as u8)
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .expect("tokio runtime")
}

fn run(args: &[String]) -> Result<ExitCode, String> {
    let options = parse(args)?;
    match options.positional.first().map(String::as_str) {
        Some("serve") => {
            let path = options.named.get("spec").ok_or("serve needs --spec FILE")?;
            let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
            let value: Value = serde_json::from_str(&text).map_err(|e| format!("{path}: {e}"))?;
            let spec = Spec::from_json(&value)?;
            let ephemeral = options.named.contains_key("ephemeral");
            let runtime = runtime();
            let code = runtime.block_on(async move {
                let Some(http) = spec.http.clone() else {
                    return daemon::serve(spec, ephemeral).await;
                };
                let settings = match http::Settings::from_spec(&http) {
                    Ok(settings) => settings,
                    Err(message) => {
                        eprintln!("E_CONFIG: {message}");
                        return EXIT_CONFIG;
                    }
                };
                let prepare: daemon::Prepare = Box::new(move || {
                    let listener = http::bind(&settings)
                        .map_err(|error| format!("the HTTP port {}: {error}", settings.port))?;
                    let front: daemon::FrontEnd =
                        Box::new(move |gateway| http::start(settings, listener, gateway));
                    Ok(front)
                });
                daemon::serve_with(spec, ephemeral, Some(prepare)).await
            });
            // Dropping the runtime would wait for a task that never yields (a stalled router) and keep the
            // process from exiting; past the timeout the process exits and leaves such a thread behind.
            runtime.shutdown_timeout(Duration::from_secs(5));
            Ok(ExitCode::from(code as u8))
        }
        Some("embed") => {
            let tokenizer = options
                .named
                .get("tokenizer")
                .ok_or("embed needs --tokenizer FILE")?;
            let additions =
                embed::additions(options.named.get("additions").map_or("[]", String::as_str))?;
            let preparation = options
                .named
                .get("preparation")
                .cloned()
                .unwrap_or_default();
            let provider = provider::Config::parse(
                options.named.get("provider-command").map(String::as_str),
                options.named.get("provider-env").map(String::as_str),
            )?;
            let code = embed::serve(embed::Options {
                tokenizer: PathBuf::from(tokenizer),
                additions,
                preparation,
                provider,
            });
            Ok(ExitCode::from(code as u8))
        }
        Some("status") => {
            let socket = socket(&options)?;
            let request = client::Request {
                op: "daemon.status",
                body: json!({}),
                lane: None,
                deadline: Duration::from_secs(5),
                key: None,
            };
            Ok(print_reply(
                runtime().block_on(client::call(&socket, request)),
            ))
        }
        Some("call") => {
            let op = options.positional.get(1).ok_or("call needs OP")?;
            let socket = socket(&options)?;
            let body = match options.named.get("body") {
                Some(text) => serde_json::from_str(text).map_err(|e| format!("--body: {e}"))?,
                None => json!({}),
            };
            let deadline = options
                .named
                .get("deadline-ms")
                .map(|v| v.parse::<u64>().map_err(|e| format!("--deadline-ms: {e}")))
                .transpose()?
                .unwrap_or(30_000);
            let request = client::Request {
                op,
                body,
                lane: options.named.get("lane").map(String::as_str),
                deadline: Duration::from_millis(deadline),
                key: options.named.get("key").map(String::as_str),
            };
            Ok(print_reply(
                runtime().block_on(client::call(&socket, request)),
            ))
        }
        Some("attach") => {
            let client_id = options
                .named
                .get("client-id")
                .ok_or("attach needs --client-id ID")?;
            if client_id.is_empty() || client_id.len() > 128 {
                return Err("attach: --client-id must be 1 to 128 characters".into());
            }
            let protocol = match options.named.get("protocol").map(String::as_str) {
                None | Some("1") => 1,
                Some("2") => 2,
                Some("auto") => 0,
                Some(other) => return Err(format!("attach: unknown protocol {other}")),
            };
            let socket = socket(&options)?;
            Ok(bridge(attach::run(&socket, client_id, protocol)))
        }
        Some("hook") => {
            // A hook never fails a session: every error, a bad path included, is reported with status 0.
            let Some(action) = options.positional.get(1).cloned() else {
                eprintln!("E_INPUT: hook needs ACTION");
                return Ok(ExitCode::SUCCESS);
            };
            let socket = match socket(&options) {
                Ok(socket) => Some(socket),
                Err(message) => {
                    eprintln!("E_RUNTIME: {message}");
                    None
                }
            };
            let options = hook::Options {
                action,
                provider: options.named.get("provider").cloned(),
                socket,
                state: options.named.get("state").map(PathBuf::from),
            };
            Ok(bridge(hook::run(options)))
        }
        Some("http-headers") => {
            let state = options
                .named
                .get("state")
                .ok_or("http-headers needs --state DIR")?;
            let port: u16 = options
                .named
                .get("port")
                .ok_or("http-headers needs --port N")?
                .parse()
                .map_err(|e| format!("--port: {e}"))?;
            match http::headers(&PathBuf::from(state), &runtime_dir(&options)?, port) {
                Ok(headers) => {
                    println!("{headers}");
                    Ok(ExitCode::SUCCESS)
                }
                Err(message) => {
                    eprintln!("{message}");
                    Ok(ExitCode::from(1))
                }
            }
        }
        Some("mcp-stdio") => {
            let runtime_dir = runtime_dir(&options)?;
            Ok(bridge(http::stdio(&runtime_dir)))
        }
        _ => Err(USAGE.into()),
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if matches!(args.as_slice(), [flag] if flag == "--version" || flag == "-V") {
        println!("kelmod {}", package_version());
        return ExitCode::SUCCESS;
    }
    match run(&args) {
        Ok(code) => code,
        Err(message) => {
            eprintln!("{message}");
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::pep440;

    #[test]
    fn cargo_versions_map_to_pep440() {
        assert_eq!(pep440("0.2.0"), "0.2.0");
        assert_eq!(pep440("0.2.0-dev.0"), "0.2.0.dev0");
        assert_eq!(pep440("0.2.0-rc.1"), "0.2.0rc1");
        assert_eq!(pep440("0.2.0-alpha.2"), "0.2.0a2");
    }
}
