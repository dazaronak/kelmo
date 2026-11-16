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
/// recycle can be dated from the log.
#[macro_export]
macro_rules! logln {
    ($($arg:tt)*) => {
        eprintln!("{} {}", $crate::stamp(std::time::SystemTime::now()), format_args!($($arg)*))
    };
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
    use super::stamp;
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
}
