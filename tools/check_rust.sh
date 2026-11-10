#!/bin/sh
# Runs the Rust checks the CI job runs: formatting, clippy with warnings denied, and the tests.
# On macOS build scripts get the system C compiler by absolute path (see tools/build_kelmod.sh).
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
if [ "$(uname -s)" = Darwin ]; then
    export CC=/usr/bin/cc CXX=/usr/bin/c++
fi
cd "$root"
manifest=rust/Cargo.toml
cargo fmt --manifest-path "$manifest" --all --check
cargo clippy --locked --manifest-path "$manifest" --workspace --all-targets -- -D warnings
cargo test --locked --manifest-path "$manifest" --workspace
