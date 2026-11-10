//! The token window: how many tokens the model sees for one text, with the semantics of the Python
//! `TokenWindow.count` (src/kelmo/store/memory/embed.py): prefix plus text, special tokens added, truncation
//! and padding switched off and checked, and an encoding that still reports overflow refused.
//!
//! `identity` is the sha256 of the exact tokenizer.json bytes; `preparation` is the digest of the special tokens
//! added on top, computed by the caller exactly as Python's `preparation_digest` does and carried through.

use std::sync::Mutex;

use sha2::{Digest, Sha256};
use tokenizers::{AddedToken, Tokenizer};

#[derive(Debug)]
pub struct WindowError {
    pub reason: &'static str,
    pub message: String,
}

impl WindowError {
    fn new(reason: &'static str, message: impl Into<String>) -> Self {
        WindowError {
            reason,
            message: message.into(),
        }
    }
}

/// One special token as Python's `special_token_additions` describes it: a bare string, or the fields of an
/// `AddedToken`. Python's `Tokenizer.add_special_tokens` marks every token special, and an `AddedToken` whose
/// `normalized` was not given takes `!special`, so false; the same rules hold here.
#[derive(Debug, Clone)]
pub struct Addition {
    pub content: String,
    pub single_word: bool,
    pub lstrip: bool,
    pub rstrip: bool,
    pub normalized: Option<bool>,
}

impl Addition {
    pub fn plain(content: &str) -> Self {
        Addition {
            content: content.to_owned(),
            single_word: false,
            lstrip: false,
            rstrip: false,
            normalized: None,
        }
    }

    fn token(&self) -> AddedToken {
        AddedToken::from(self.content.clone(), true)
            .single_word(self.single_word)
            .lstrip(self.lstrip)
            .rstrip(self.rstrip)
            .normalized(self.normalized.unwrap_or(false))
    }
}

pub struct Window {
    tokenizer: Mutex<Tokenizer>,
    pub identity: String,
}

impl Window {
    /// Parses tokenizer.json bytes, adds the special tokens in order (as the embedding loader does), and
    /// switches truncation and padding off.
    pub fn from_bytes(data: &[u8], additions: &[Addition]) -> Result<Self, WindowError> {
        let identity = format!("sha256:{:x}", Sha256::digest(data));
        let mut tokenizer = Tokenizer::from_bytes(data).map_err(|e| {
            WindowError::new(
                "tokenizer_unusable",
                format!("unreadable tokenizer.json: {e}"),
            )
        })?;
        for addition in additions {
            tokenizer.add_special_tokens(&[addition.token()]);
        }
        tokenizer
            .with_truncation(None)
            .map_err(|e| WindowError::new("tokenizer_unusable", e.to_string()))?;
        tokenizer.with_padding(None);
        if tokenizer.get_truncation().is_some() || tokenizer.get_padding().is_some() {
            return Err(WindowError::new(
                "tokenizer_unusable",
                "The tokenizer still truncates or pads",
            ));
        }
        Ok(Window {
            tokenizer: Mutex::new(tokenizer),
            identity,
        })
    }

    /// Token count of `prefix + text`, special tokens included, never truncated.
    pub fn count(&self, prefix: &str, text: &str) -> Result<usize, WindowError> {
        let input = format!("{prefix}{text}");
        let tokenizer = self.tokenizer.lock().map_err(|_| {
            WindowError::new("tokenizer_unusable", "The tokenizer lock was poisoned")
        })?;
        let encoding = tokenizer
            .encode(input.as_str(), true)
            .map_err(|e| WindowError::new("tokenizer_unusable", e.to_string()))?;
        if !encoding.get_overflowing().is_empty() {
            return Err(WindowError::new(
                "tokenizer_unusable",
                "The tokenizer truncated a count",
            ));
        }
        Ok(encoding.get_ids().len())
    }
}
