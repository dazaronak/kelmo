//! Canonical JSON, byte for byte what Python writes with
//! `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)`.
//!
//! Written by hand rather than through `serde_json::to_string`, because the bytes are a contract: object keys
//! are sorted here whatever features a dependency turns on, and numbers are spelled as Python's `repr` spells
//! them (`1e-05`, `1e+16`, `0.1`, `2.0`).

use serde_json::{Number, Value};

use crate::WireError;

/// The canonical text of `value`.
pub fn to_string(value: &Value) -> Result<String, WireError> {
    let mut out = String::new();
    write_value(&mut out, value)?;
    Ok(out)
}

fn write_value(out: &mut String, value: &Value) -> Result<(), WireError> {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(true) => out.push_str("true"),
        Value::Bool(false) => out.push_str("false"),
        Value::Number(number) => write_number(out, number)?,
        Value::String(text) => write_string(out, text),
        Value::Array(items) => {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_value(out, item)?;
            }
            out.push(']');
        }
        Value::Object(map) => {
            // Python sorts str keys by code point; for UTF-8 text that is byte order.
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort_unstable_by(|a, b| a.as_bytes().cmp(b.as_bytes()));
            out.push('{');
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                write_string(out, key);
                out.push(':');
                write_value(out, &map[key])?;
            }
            out.push('}');
        }
    }
    Ok(())
}

fn write_string(out: &mut String, text: &str) {
    out.push('"');
    for character in text.chars() {
        match character {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn write_number(out: &mut String, number: &Number) -> Result<(), WireError> {
    if let Some(value) = number.as_i64() {
        out.push_str(&value.to_string());
    } else if let Some(value) = number.as_u64() {
        out.push_str(&value.to_string());
    } else {
        let value = number
            .as_f64()
            .ok_or_else(|| WireError::protocol("Number is not representable."))?;
        out.push_str(&python_float(value)?);
    }
    Ok(())
}

/// A float as Python's `repr` writes it: the shortest digits that round-trip, positional between 1e-4 and
/// 1e16, otherwise scientific with a signed, at least two-digit exponent.
pub fn python_float(value: f64) -> Result<String, WireError> {
    if !value.is_finite() {
        return Err(WireError::protocol("NaN and infinity are not JSON."));
    }
    // `{:e}` gives the shortest round-trip digits: "1.2345e3", "-5e-7", "0e0".
    let scientific = format!("{:e}", value.abs());
    let (mantissa, exponent) = scientific
        .split_once('e')
        .ok_or_else(|| WireError::protocol("Float formatting failed."))?;
    let exponent: i32 = exponent
        .parse()
        .map_err(|_| WireError::protocol("Float formatting failed."))?;
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let (digits, exponent) = half_even(value.abs(), digits, exponent);
    let decimal_point = exponent + 1;
    let sign = if value.is_sign_negative() { "-" } else { "" };
    let body = if !(-4 < decimal_point && decimal_point <= 16) {
        let mut text = digits[..1].to_owned();
        if digits.len() > 1 {
            text.push('.');
            text.push_str(&digits[1..]);
        }
        let shown = decimal_point - 1;
        let exponent_sign = if shown < 0 { '-' } else { '+' };
        format!("{text}e{exponent_sign}{:02}", shown.abs())
    } else if decimal_point <= 0 {
        format!("0.{}{}", "0".repeat((-decimal_point) as usize), digits)
    } else if decimal_point as usize >= digits.len() {
        format!(
            "{}{}.0",
            digits,
            "0".repeat(decimal_point as usize - digits.len())
        )
    } else {
        let split = decimal_point as usize;
        format!("{}.{}", &digits[..split], &digits[split..])
    };
    Ok(format!("{sign}{body}"))
}

/// Python and Rust both write the shortest digits that read back as the same double, but when the double
/// lies exactly halfway between two such candidates Python takes the one with the even last digit and Rust
/// rounds up. The exact decimal expansion of a double is finite, so a tie is visible in it: the shortest
/// digits are followed by a single 5.
fn half_even(value: f64, digits: String, exponent: i32) -> (String, i32) {
    let exact = format!("{value:.1100e}");
    let Some((mantissa, exact_exponent)) = exact.split_once('e') else {
        return (digits, exponent);
    };
    let Ok(exact_exponent) = exact_exponent.parse::<i32>() else {
        return (digits, exponent);
    };
    let full: String = mantissa.chars().filter(|c| *c != '.').collect();
    let full = full.trim_end_matches('0');
    let n = digits.len();
    if exact_exponent != exponent || full.len() != n + 1 || !full.ends_with('5') {
        return (digits, exponent);
    }
    let lower = &full[..n];
    let even = lower.as_bytes()[n - 1].is_multiple_of(2);
    let (candidate, candidate_exponent) = if even {
        (trim(lower.to_owned()), exponent)
    } else {
        increment(lower, exponent)
    };
    if candidate == digits {
        return (digits, exponent);
    }
    let reads_back = format!(
        "{}.{}e{}",
        &candidate[..1],
        &candidate[1..],
        candidate_exponent
    )
    .parse::<f64>()
    .is_ok_and(|parsed| parsed == value);
    if reads_back {
        (candidate, candidate_exponent)
    } else {
        (digits, exponent)
    }
}

/// The decimal digit string plus one in its last place, with the exponent a carry out of the first digit
/// raises ("19" -> "2", "99" -> "1" one place up).
fn increment(digits: &str, exponent: i32) -> (String, i32) {
    let mut bytes = digits.as_bytes().to_vec();
    let mut index = bytes.len();
    loop {
        if index == 0 {
            bytes.insert(0, b'1');
            bytes.pop();
            let text = String::from_utf8(bytes).unwrap_or_default();
            return (trim(text), exponent + 1);
        }
        index -= 1;
        if bytes[index] == b'9' {
            bytes[index] = b'0';
        } else {
            bytes[index] += 1;
            break;
        }
    }
    (trim(String::from_utf8(bytes).unwrap_or_default()), exponent)
}

fn trim(digits: String) -> String {
    let trimmed = digits.trim_end_matches('0');
    if trimmed.is_empty() {
        "0".to_owned()
    } else {
        trimmed.to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn floats_are_spelled_as_python_repr() {
        let cases = [
            (0.0, "0.0"),
            (-0.0, "-0.0"),
            (1.0, "1.0"),
            (0.1, "0.1"),
            (1e-5, "1e-05"),
            (0.0001, "0.0001"),
            (1e16, "1e+16"),
            (1234567890123456.0, "1234567890123456.0"),
            (12345678901234567.0, "1.2345678901234568e+16"),
            (-2.5e-7, "-2.5e-07"),
            (1.5e300, "1.5e+300"),
            (123.456, "123.456"),
            // exactly ...379.25: a tie between .2 and .3, which Python breaks to the even digit
            (889502080264379.2, "889502080264379.2"),
            (2.5, "2.5"),
        ];
        for (value, expected) in cases {
            assert_eq!(python_float(value).unwrap(), expected, "{value}");
        }
    }

    #[test]
    fn keys_are_sorted_and_strings_escaped_as_python() {
        let value = json!({"b": 1, "a": [true, null, "q\"\\\n\u{1}\u{7f}é\u{2028}😀"], "A": {}});
        assert_eq!(
            to_string(&value).unwrap(),
            "{\"A\":{},\"a\":[true,null,\"q\\\"\\\\\\n\\u0001\u{7f}é\u{2028}😀\"],\"b\":1}"
        );
    }
}
