use std::collections::HashMap;

use crate::error::{Error, Result};
use crate::protobuf::wire::{self, WireType};

const MAX_PROTOBUF_NESTING_DEPTH: usize = 64;

/// A parsed protobuf value.
#[derive(Debug, Clone)]
#[allow(dead_code)]
pub enum ProtoValue {
    Varint(u64),
    Fixed32(u32),
    Fixed64(u64),
    Bytes(Vec<u8>),
    /// A length-delimited field that was successfully parsed as a nested message.
    /// The original bytes are preserved so callers can access raw data if needed.
    Message(ProtoMessage, Vec<u8>),
}

/// A parsed protobuf message — a map from field numbers to lists of values.
#[derive(Debug, Clone)]
pub struct ProtoMessage {
    pub fields: HashMap<u32, Vec<ProtoValue>>,
}

impl ProtoMessage {
    /// Parse raw bytes into a ProtoMessage.
    /// Length-delimited fields are speculatively parsed as nested messages;
    /// if that fails they're stored as Bytes.
    pub fn parse(data: &[u8]) -> Result<Self> {
        Self::parse_with_depth(data, 0)
    }

    fn parse_with_depth(data: &[u8], depth: usize) -> Result<Self> {
        if depth > MAX_PROTOBUF_NESTING_DEPTH {
            return Err(Error::ProtobufDecode(format!(
                "max protobuf nesting depth exceeded ({MAX_PROTOBUF_NESTING_DEPTH})"
            )));
        }

        let mut fields: HashMap<u32, Vec<ProtoValue>> = HashMap::new();
        let mut pos = 0;

        while pos < data.len() {
            let (field_number, wire_type, key_len) = wire::read_field_key(&data[pos..])
                .map_err(|e| Error::ProtobufDecode(format!("at offset {pos}: {e}")))?;
            pos += key_len;

            let value = match wire_type {
                WireType::Varint => {
                    let (val, consumed) = wire::read_varint(&data[pos..]).map_err(|e| {
                        Error::ProtobufDecode(format!("field {field_number} varint at {pos}: {e}"))
                    })?;
                    pos += consumed;
                    ProtoValue::Varint(val)
                }
                WireType::Fixed64 => {
                    let end = match pos.checked_add(8) {
                        Some(end) => end,
                        None => {
                            return Err(Error::ProtobufDecode(format!(
                                "field {field_number} fixed64 length overflow at {pos}"
                            )));
                        }
                    };
                    if end > data.len() {
                        return Err(Error::Truncated {
                            expected: end,
                            actual: data.len(),
                        });
                    }
                    let val = u64::from_le_bytes(data[pos..end].try_into().unwrap());
                    pos = end;
                    ProtoValue::Fixed64(val)
                }
                WireType::Fixed32 => {
                    let end = match pos.checked_add(4) {
                        Some(end) => end,
                        None => {
                            return Err(Error::ProtobufDecode(format!(
                                "field {field_number} fixed32 length overflow at {pos}"
                            )));
                        }
                    };
                    if end > data.len() {
                        return Err(Error::Truncated {
                            expected: end,
                            actual: data.len(),
                        });
                    }
                    let val = u32::from_le_bytes(data[pos..end].try_into().unwrap());
                    pos = end;
                    ProtoValue::Fixed32(val)
                }
                WireType::LengthDelimited => {
                    let (len, consumed) = wire::read_varint(&data[pos..]).map_err(|e| {
                        Error::ProtobufDecode(format!("field {field_number} length at {pos}: {e}"))
                    })?;
                    pos += consumed;
                    let len = len as usize;
                    let end = match pos.checked_add(len) {
                        Some(end) => end,
                        None => {
                            return Err(Error::ProtobufDecode(format!(
                                "field {field_number} length overflow at {pos}"
                            )));
                        }
                    };
                    if end > data.len() {
                        return Err(Error::Truncated {
                            expected: end,
                            actual: data.len(),
                        });
                    }
                    let bytes = &data[pos..end];
                    pos = end;

                    // Speculatively try to parse as a nested message
                    let raw = bytes.to_vec();
                    if let Ok(msg) = ProtoMessage::parse_with_depth(bytes, depth + 1) {
                        ProtoValue::Message(msg, raw)
                    } else {
                        ProtoValue::Bytes(raw)
                    }
                }
            };

            fields.entry(field_number).or_default().push(value);
        }

        Ok(ProtoMessage { fields })
    }

    /// Get the first value for a field.
    pub fn get(&self, field: u32) -> Option<&ProtoValue> {
        self.fields.get(&field).and_then(|v| v.first())
    }

    /// Get all values for a field.
    pub fn get_all(&self, field: u32) -> &[ProtoValue] {
        self.fields.get(&field).map(|v| v.as_slice()).unwrap_or(&[])
    }

    /// Get a varint field value.
    pub fn get_varint(&self, field: u32) -> Option<u64> {
        match self.get(field)? {
            ProtoValue::Varint(v) => Some(*v),
            _ => None,
        }
    }

    /// Get a fixed32 field value.
    pub fn get_fixed32(&self, field: u32) -> Option<u32> {
        match self.get(field)? {
            ProtoValue::Fixed32(v) => Some(*v),
            _ => None,
        }
    }

    /// Get raw bytes from a length-delimited field.
    /// Returns the original bytes even if the field was parsed as a nested message.
    #[allow(dead_code)]
    pub fn get_bytes(&self, field: u32) -> Option<&[u8]> {
        match self.get(field)? {
            ProtoValue::Bytes(b) => Some(b),
            ProtoValue::Message(_, raw) => Some(raw),
            _ => None,
        }
    }

    /// Get a nested message.
    #[allow(dead_code)]
    pub fn get_message(&self, field: u32) -> Option<&ProtoMessage> {
        match self.get(field)? {
            ProtoValue::Message(m, _) => Some(m),
            _ => None,
        }
    }

    /// Get all nested messages for a field.
    pub fn get_messages(&self, field: u32) -> Vec<&ProtoMessage> {
        self.get_all(field)
            .iter()
            .filter_map(|v| match v {
                ProtoValue::Message(m, _) => Some(m),
                _ => None,
            })
            .collect()
    }

    /// Try to interpret a length-delimited field as a UTF-8 string.
    /// Works even if the field was speculatively parsed as a message.
    pub fn get_string(&self, field: u32) -> Option<String> {
        match self.get(field)? {
            ProtoValue::Bytes(b) => String::from_utf8(b.clone()).ok(),
            ProtoValue::Message(_, raw) => String::from_utf8(raw.clone()).ok(),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_simple_varint() {
        // field 1, varint, value 150
        // key = (1 << 3) | 0 = 0x08
        // 150 = 0x96 0x01
        let data = [0x08, 0x96, 0x01];
        let msg = ProtoMessage::parse(&data).unwrap();
        assert_eq!(msg.get_varint(1), Some(150));
    }

    #[test]
    fn test_parse_length_delimited() {
        // field 2, length-delimited, "testing"
        // key = (2 << 3) | 2 = 0x12
        // length = 7
        let data = [0x12, 0x07, b't', b'e', b's', b't', b'i', b'n', b'g'];
        let msg = ProtoMessage::parse(&data).unwrap();
        // Should be stored as bytes (not valid protobuf)
        let bytes = msg.get_bytes(2).unwrap();
        assert_eq!(bytes, b"testing");
    }

    #[test]
    fn test_parse_nested_message() {
        // Nested: field 1 = varint 42
        // inner: 0x08 0x2A
        // Outer: field 3, length-delimited, containing inner
        // key = (3 << 3) | 2 = 0x1A, length = 2
        let data = [0x1A, 0x02, 0x08, 0x2A];
        let msg = ProtoMessage::parse(&data).unwrap();
        let inner = msg.get_message(3).unwrap();
        assert_eq!(inner.get_varint(1), Some(42));
    }

    #[test]
    fn test_multiple_values_same_field() {
        // field 1 = 1, field 1 = 2
        let data = [0x08, 0x01, 0x08, 0x02];
        let msg = ProtoMessage::parse(&data).unwrap();
        let all = msg.get_all(1);
        assert_eq!(all.len(), 2);
    }

    fn encode_varint(mut value: u64) -> Vec<u8> {
        let mut out = Vec::new();
        loop {
            let mut byte = (value & 0x7F) as u8;
            value >>= 7;
            if value != 0 {
                byte |= 0x80;
            }
            out.push(byte);
            if value == 0 {
                break;
            }
        }
        out
    }

    fn wrap_in_field_1(mut bytes: Vec<u8>) -> Vec<u8> {
        let mut out = Vec::new();
        out.push(0x0A);
        out.extend_from_slice(&encode_varint(bytes.len() as u64));
        out.append(&mut bytes);
        out
    }

    #[test]
    fn test_deeply_nested_message_does_not_stack_overflow() {
        // Build a message nested far deeper than the configured recursion cap.
        let mut data = vec![0x08, 0x01];
        for _ in 0..256 {
            data = wrap_in_field_1(data);
        }
        let parsed = ProtoMessage::parse(&data).expect("parse should succeed");
        assert!(!parsed.get_all(1).is_empty());
    }

    #[test]
    fn test_parse_rejects_length_overflow_without_panicking() {
        // field 3, length-delimited, length=u64::MAX
        let data = [
            0x1A, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01, 0x00,
        ];
        assert!(ProtoMessage::parse(&data).is_err());
    }
}
