#!/bin/sh
# Builds the kelmod binary into rust/target/<profile>/kelmod.
# Usage: tools/build_kelmod.sh [--release] [cargo arguments...]
#
# On macOS build scripts get the system C compiler by absolute path, because a `cc` on PATH is not
# always a C compiler; the linker is set the same way in .cargo/config.toml.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)
if [ "$(uname -s)" = Darwin ]; then
    export CC=/usr/bin/cc CXX=/usr/bin/c++
fi
cd "$root"
exec cargo build --locked --manifest-path rust/Cargo.toml --bin kelmod "$@"
