use crate::error::{Error, Result};

const XHR1_MAGIC: &[u8] = b"XHR1";
const XOR_KEY: u8 = 0x9b;

/// A single record extracted from an XHR1 container.
#[derive(Debug)]
#[allow(dead_code)]
pub struct XhrRecord {
    pub index: u8,
    pub flags: u8,
    pub protobuf_data: Vec<u8>,
}

/// Parse an XHR1 container into its constituent records.
///
/// Format:
/// - 4 bytes: "XHR1" magic
/// - Repeated records:
///   - 4 bytes big-endian: record length (N)
///   - N bytes: record data
///     - byte[0]: record index
///     - byte[1]: flags
///     - bytes[2..]: XOR-encrypted protobuf data (key = 0x9b)
pub fn parse_xhr1(data: &[u8]) -> Result<Vec<XhrRecord>> {
    if data.len() < 4 || &data[..4] != XHR1_MAGIC {
        return Err(Error::Xhr1MissingMagic);
    }

    let mut pos = 4;
    let mut records = Vec::new();

    while pos < data.len() {
        let len_field_end = pos.checked_add(4).ok_or_else(|| {
            Error::Xhr1Parse(format!("record length prefix overflow at offset {pos}"))
        })?;
        if len_field_end > data.len() {
            return Err(Error::Xhr1Parse(format!(
                "truncated length at offset {pos}"
            )));
        }

        let len = u32::from_be_bytes(data[pos..len_field_end].try_into().unwrap()) as usize;
        pos = len_field_end;

        let record_end = pos
            .checked_add(len)
            .ok_or_else(|| Error::Xhr1Parse(format!("record length overflow at offset {pos}")))?;
        if record_end > data.len() {
            return Err(Error::Truncated {
                expected: record_end,
                actual: data.len(),
            });
        }

        if len < 2 {
            pos = record_end;
            continue;
        }

        let record_data = &data[pos..record_end];
        let index = record_data[0];
        let flags = record_data[1];

        // XOR decrypt the protobuf payload
        let protobuf_data: Vec<u8> = record_data[2..].iter().map(|b| b ^ XOR_KEY).collect();

        records.push(XhrRecord {
            index,
            flags,
            protobuf_data,
        });

        pos = record_end;
    }

    Ok(records)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_missing_magic() {
        assert!(parse_xhr1(b"NOPE").is_err());
    }

    #[test]
    fn test_empty_container() {
        let records = parse_xhr1(b"XHR1").unwrap();
        assert!(records.is_empty());
    }

    #[test]
    fn test_single_record() {
        let mut data = b"XHR1".to_vec();
        // Record: index=0, flags=1, payload=[0xAA, 0xBB]
        let record = [0x00, 0x01, 0xAA, 0xBB];
        data.extend_from_slice(&(record.len() as u32).to_be_bytes());
        data.extend_from_slice(&record);

        let records = parse_xhr1(&data).unwrap();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].index, 0);
        assert_eq!(records[0].flags, 1);
        // payload bytes XOR'd with 0x9b
        assert_eq!(records[0].protobuf_data, vec![0xAA ^ 0x9b, 0xBB ^ 0x9b]);
    }

    #[test]
    fn test_xor_roundtrip() {
        let original = vec![0x08, 0x96, 0x01]; // a varint
        let encrypted: Vec<u8> = original.iter().map(|b| b ^ XOR_KEY).collect();
        let decrypted: Vec<u8> = encrypted.iter().map(|b| b ^ XOR_KEY).collect();
        assert_eq!(original, decrypted);
    }
}
