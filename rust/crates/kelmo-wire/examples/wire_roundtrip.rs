//! Reads kelmo/2 frames on stdin and writes each one back, decoded and encoded again, on stdout.
//! The cross-language fuzz test feeds it Python-encoded frames and compares the bytes.

use std::io::{Read, Write};

fn main() {
    let mut input = Vec::new();
    std::io::stdin().read_to_end(&mut input).expect("stdin");
    let mut reader = kelmo_wire::FrameReader::new();
    let frames = match reader.feed(&input) {
        Ok(frames) => frames,
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    };
    if reader.pending() != 0 {
        eprintln!("E_PROTOCOL: {} trailing bytes", reader.pending());
        std::process::exit(1);
    }
    let mut out = std::io::stdout().lock();
    for (header, body) in frames {
        match kelmo_wire::encode(&header, &body) {
            Ok(bytes) => out.write_all(&bytes).expect("stdout"),
            Err(error) => {
                eprintln!("{error}");
                std::process::exit(1);
            }
        }
    }
}
