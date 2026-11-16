//! Mailboxes, lanes, router, supervisor and local IPC of the Kelmo daemon.

pub mod client;
pub mod daemon;
pub mod gateway;
pub mod ipc;
pub mod lane;
pub mod router;
pub mod spec;
pub mod supervisor;
pub mod sys;

/// Writes one line to the daemon's log (its standard error), led by the UTC time, so that a restart or a
/// recycle can be dated from the log. A line separator inside the message is written escaped, and the whole
/// line goes out in one write, so a core writing to the same log cannot split it: every line of the log that
/// kelmod writes starts with its time. A failed write is dropped; logging never stops the daemon.
#[macro_export]
macro_rules! logln {
    ($($arg:tt)*) => {
        $crate::write_log_line(&format!($($arg)*))
    };
}

/// The line `logln!` writes: the time, a space, the message on one line, a line feed; one `write_all`.
pub fn write_log_line(message: &str) {
    use std::io::Write;
    let line = format!(
        "{} {}\n",
        stamp(std::time::SystemTime::now()),
        one_line(message)
    );
    let _ = std::io::stderr().lock().write_all(line.as_bytes());
}

/// `text` with every character a reader may take for a line break (CR, LF, vertical tab, form feed, the
/// separators 0x1C-0x1E, NEL, U+2028, U+2029) written as its escape, such as `\n` or `\u{2028}`.
pub fn one_line(text: &str) -> String {
    text.chars()
        .map(|c| match c {
            '\r'
            | '\n'
            | '\u{b}'
            | '\u{c}'
            | '\u{1c}'..='\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}' => c.escape_default().to_string(),
            _ => c.to_string(),
        })
        .collect()
}

/// `at` as `YYYY-MM-DDTHH:MM:SS.mmmZ`, in UTC. A time before 1970 is written as the epoch.
pub fn stamp(at: std::time::SystemTime) -> String {
    let since = at.duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
    let seconds = since.as_secs();
    let (days, rest) = ((seconds / 86_400) as i64, seconds % 86_400);
    // Days since 1970-01-01 to a civil date (proleptic Gregorian calendar).
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}.{:03}Z",
        rest / 3_600,
        rest % 3_600 / 60,
        rest % 60,
        since.subsec_millis()
    )
}

#[cfg(test)]
mod stamp_tests {
    use super::{one_line, stamp};
    use std::time::{Duration, UNIX_EPOCH};

    #[test]
    fn a_time_is_written_in_utc_with_milliseconds() {
        assert_eq!(
            stamp(UNIX_EPOCH + Duration::from_millis(1_763_278_800_250)),
            "2025-11-16T07:40:00.250Z"
        );
        assert_eq!(
            stamp(UNIX_EPOCH + Duration::from_secs(951_782_400)),
            "2000-02-29T00:00:00.000Z"
        );
        assert_eq!(stamp(UNIX_EPOCH), "1970-01-01T00:00:00.000Z");
    }

    #[test]
    fn a_line_break_in_a_message_stays_on_the_one_line() {
        assert_eq!(
            one_line("core state\nkelmod: forged\r"),
            "core state\\nkelmod: forged\\r"
        );
        assert_eq!(
            one_line("core state uses 279 MiB"),
            "core state uses 279 MiB"
        );
        for separator in ['\u{b}', '\u{c}', '\u{1c}', '\u{85}', '\u{2028}', '\u{2029}'] {
            let escaped = one_line(&format!("core{separator}state"));
            assert!(
                !escaped.contains(separator)
                    && escaped.starts_with("core")
                    && escaped.ends_with("state"),
                "{escaped:?}"
            );
        }
    }
}
