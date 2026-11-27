//! JSON parsing and serialization owned by the Rust engine.
//!
//! The Python bridge may extract or construct primitive JSON-compatible values,
//! but it must not invoke a Python JSON implementation or select encoding rules.

pub use serde_json::{Map as JsonMap, Number as JsonNumber, Value as JsonValue};

pub fn parse(bytes: &[u8]) -> Result<JsonValue, serde_json::Error> {
    serde_json::from_slice(bytes)
}

pub fn serialize_pretty(value: &JsonValue) -> Result<Vec<u8>, serde_json::Error> {
    serde_json::to_vec_pretty(value)
}

pub fn serialize(value: &JsonValue) -> Result<Vec<u8>, serde_json::Error> {
    serde_json::to_vec(value)
}
