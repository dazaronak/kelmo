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
/// recycle can be dated from the log. A line separator or other control character inside the message is
/// written escaped, and the whole line is handed to the log in one write, so a core writing to the same log
/// does not split it as a rule. A failed write is dropped (logging never stops the daemon); after one cut part
/// way the next line starts on a fresh line. Rust's own panic message is not written through here, and a line
/// follows whatever a core left unfinished on the same log.
#[macro_export]
macro_rules! logln {
    ($($arg:tt)*) => {
        $crate::write_log_line(&format!($($arg)*))
    };
}

/// The line `logln!` writes: the time, a space, the message on one line, a line feed.
pub fn write_log_line(message: &str) {
    static BROKEN: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
    emit_log_line(&mut std::io::stderr().lock(), message, &BROKEN);
}

/// How many times a line waits a millisecond for a log that answers it would block.
const LOG_WOULD_BLOCK_TRIES: u32 = 50;

/// Writes one stamped line to `sink`, already locked by the caller, so the time is read in the order the
/// lines are written. After a write that failed part way (`broken`), the line starts with a line feed, so it
/// does not continue the cut one. A sink that would block is waited for briefly; then the line is dropped.
pub fn emit_log_line(
    sink: &mut dyn std::io::Write,
    message: &str,
    broken: &std::sync::atomic::AtomicBool,
) {
    use std::sync::atomic::Ordering;
    let lead = if broken.load(Ordering::Relaxed) {
        "\n"
    } else {
        ""
    };
    let line = format!(
        "{lead}{} {}\n",
        stamp(std::time::SystemTime::now()),
        one_line(message)
    );
    let mut rest = line.as_bytes();
    let mut tries = 0;
    while !rest.is_empty() {
        match sink.write(rest) {
            Ok(0) => break,
            Ok(n) => rest = &rest[n..],
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
            Err(error)
                if error.kind() == std::io::ErrorKind::WouldBlock
                    && tries < LOG_WOULD_BLOCK_TRIES =>
            {
                tries += 1;
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
            Err(_) => break,
        }
    }
    // Cut part way: the next line must not continue it. Nothing written at all: nothing to end, so a line cut
    // earlier still waits for its line feed and one that was whole stays whole.
    if rest.is_empty() {
        broken.store(false, Ordering::Relaxed);
    } else if rest.len() < line.len() {
        broken.store(true, Ordering::Relaxed);
    }
}

/// `text` with every control character (line breaks, tabs, escape sequences a terminal would act on) and the
/// separators U+2028 and U+2029 written as its escape, such as `\n` or `\u{1b}`, and a backslash as `\\`,
/// so an escaped break cannot be told apart from text that spells one.
pub fn one_line(text: &str) -> String {
    text.chars()
        .map(|c| match c {
            '\\' | '\u{2028}' | '\u{2029}' => c.escape_default().to_string(),
            c if c.is_control() => c.escape_default().to_string(),
            _ => c.to_string(),
        })
        .collect()
}

/// `at` as `YYYY-MM-DDTHH:MM:SS.mmmZ`, in UTC, a time before 1970 included.
pub fn stamp(at: std::time::SystemTime) -> String {
    let millis: i128 = match at.duration_since(std::time::UNIX_EPOCH) {
        Ok(since) => since.as_millis() as i128,
        // Rounded down, as after 1970: a nanosecond before the epoch is 23:59:59.999.
        Err(before) => -(before.duration().as_nanos().div_ceil(1_000_000) as i128),
    };
    let (seconds, millis) = (millis.div_euclid(1_000) as i64, millis.rem_euclid(1_000));
    let (days, rest) = (seconds.div_euclid(86_400), seconds.rem_euclid(86_400));
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
        millis
    )
}

#[cfg(test)]
mod stamp_tests {
    use super::{emit_log_line, one_line, stamp};
    use std::io::Write;
    use std::sync::atomic::AtomicBool;
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

    #[test]
    fn a_backslash_is_escaped_so_an_escaped_break_cannot_be_forged() {
        assert_eq!(one_line("a\\nb"), "a\\\\nb");
        assert_ne!(one_line("a\\nb"), one_line("a\nb"));
    }

    #[test]
    fn a_time_before_1970_is_written_as_that_time() {
        assert_eq!(
            stamp(UNIX_EPOCH - Duration::from_millis(1_500)),
            "1969-12-31T23:59:58.500Z"
        );
    }

    struct Sink {
        written: Vec<u8>,
        fail_after: Option<usize>,
        would_block: usize,
    }

    impl Write for Sink {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            if self.would_block > 0 {
                self.would_block -= 1;
                return Err(std::io::ErrorKind::WouldBlock.into());
            }
            if let Some(left) = self.fail_after {
                if left == 0 {
                    return Err(std::io::Error::other("disk full"));
                }
                let n = buf.len().min(left);
                self.fail_after = Some(left - n);
                self.written.extend_from_slice(&buf[..n]);
                return Ok(n);
            }
            self.written.extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn a_line_after_a_cut_write_starts_on_a_line_of_its_own() {
        let broken = AtomicBool::new(false);
        let mut sink = Sink {
            written: Vec::new(),
            fail_after: Some(5),
            would_block: 0,
        };
        emit_log_line(&mut sink, "first message", &broken);
        sink.fail_after = None;
        emit_log_line(&mut sink, "second message", &broken);
        let text = String::from_utf8(sink.written).unwrap();
        let last = text.lines().last().unwrap();
        assert!(last.ends_with("Z second message"), "{text:?}");
        assert_eq!(
            last.len(),
            "YYYY-MM-DDTHH:MM:SS.mmmZ second message".len(),
            "{text:?}"
        );
    }

    #[test]
    fn a_line_waits_out_a_full_nonblocking_log_briefly() {
        let broken = AtomicBool::new(false);
        let mut sink = Sink {
            written: Vec::new(),
            fail_after: None,
            would_block: 3,
        };
        emit_log_line(&mut sink, "kept", &broken);
        assert!(
            String::from_utf8(sink.written)
                .unwrap()
                .ends_with("Z kept\n")
        );
    }

    #[test]
    fn a_time_just_before_1970_rounds_down() {
        assert_eq!(
            stamp(UNIX_EPOCH - Duration::from_nanos(1)),
            "1969-12-31T23:59:59.999Z"
        );
        assert_eq!(
            stamp(UNIX_EPOCH - Duration::from_micros(1_000_500)),
            "1969-12-31T23:59:58.999Z"
        );
    }

    #[test]
    fn a_control_character_is_escaped() {
        let escaped = one_line("core\u{1b}[31mred\u{7}\u{0}\ttab");
        assert!(!escaped.chars().any(|c| c.is_control()), "{escaped:?}");
    }

    #[test]
    fn a_line_that_wrote_nothing_leaves_no_blank_line() {
        let broken = AtomicBool::new(false);
        let mut sink = Sink {
            written: Vec::new(),
            fail_after: Some(0),
            would_block: 0,
        };
        emit_log_line(&mut sink, "dropped", &broken);
        sink.fail_after = None;
        emit_log_line(&mut sink, "next", &broken);
        let text = String::from_utf8(sink.written).unwrap();
        assert!(!text.starts_with('\n'), "{text:?}");
    }
}
