//! The Kelmo daemon binary.

use std::process::ExitCode;

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

const USAGE: &str = "usage: kelmod --version";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.as_slice() {
        [flag] if flag == "--version" || flag == "-V" => {
            println!("kelmod {}", package_version());
            ExitCode::SUCCESS
        }
        _ => {
            eprintln!("{USAGE}");
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
