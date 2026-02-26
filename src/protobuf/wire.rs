use crate::error::{Error, Result};

/// Protobuf wire types.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WireType {
    Varint,          // 0
    Fixed64,         // 1
    LengthDelimited, // 2
    Fixed32,         // 5
}

impl WireType {
    pub fn from_u8(v: u8) -> Result<Self> {
        match v {
            0 => Ok(Self::Varint),
            1 => Ok(Self::Fixed64),
            2 => Ok(Self::LengthDelimited),
            5 => Ok(Self::Fixed32),
            other => Err(Error::ProtobufDecode(format!("unknown wire type {other}"))),
        }
    }
}

/// Decode a LEB128 varint from `data`. Returns (value, bytes_consumed).
pub fn read_varint(data: &[u8]) -> Result<(u64, usize)> {
    let mut result: u64 = 0;

    for (i, &byte) in data.iter().enumerate() {
        if i >= 10 {
            return Err(Error::VarintOverflow);
        }

        let low = (byte & 0x7F) as u64;
        // A u64 varint can use at most 10 bytes, and the 10th byte may only
        // carry one data bit.
        if i == 9 && ((byte & 0x80) != 0 || low > 1) {
            return Err(Error::VarintOverflow);
        }

        result |= low << (i * 7);
        if byte & 0x80 == 0 {
            return Ok((result, i + 1));
        }
    }
    Err(Error::ProtobufDecode("unterminated varint".into()))
}

/// ZigZag decode: maps unsigned back to signed.
pub fn zigzag_decode(n: u64) -> i64 {
    ((n >> 1) as i64) ^ -((n & 1) as i64)
}

/// Read a protobuf field key. Returns (field_number, wire_type, bytes_consumed).
pub fn read_field_key(data: &[u8]) -> Result<(u32, WireType, usize)> {
    let (key, consumed) = read_varint(data)?;
    let wire_type = WireType::from_u8((key & 0x07) as u8)?;
    let field_number = (key >> 3) as u32;
    if field_number == 0 {
        return Err(Error::ProtobufDecode(
            "invalid field number 0 in protobuf key".into(),
        ));
    }
    Ok((field_number, wire_type, consumed))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_varint_single_byte() {
        let (val, consumed) = read_varint(&[0x08]).unwrap();
        assert_eq!(val, 8);
        assert_eq!(consumed, 1);
    }

    #[test]
    fn test_varint_multi_byte() {
        // 300 = 0b100101100 → varint bytes: 0xAC 0x02
        let (val, consumed) = read_varint(&[0xAC, 0x02]).unwrap();
        assert_eq!(val, 300);
        assert_eq!(consumed, 2);
    }

    #[test]
    fn test_varint_zero() {
        let (val, consumed) = read_varint(&[0x00]).unwrap();
        assert_eq!(val, 0);
        assert_eq!(consumed, 1);
    }

    #[test]
    fn test_zigzag() {
        assert_eq!(zigzag_decode(0), 0);
        assert_eq!(zigzag_decode(1), -1);
        assert_eq!(zigzag_decode(2), 1);
        assert_eq!(zigzag_decode(3), -2);
        assert_eq!(zigzag_decode(4), 2);
    }

    #[test]
    fn test_field_key() {
        // field 1, varint: key = (1 << 3) | 0 = 0x08
        let (num, wt, consumed) = read_field_key(&[0x08]).unwrap();
        assert_eq!(num, 1);
        assert_eq!(wt, WireType::Varint);
        assert_eq!(consumed, 1);

        // field 2, length-delimited: key = (2 << 3) | 2 = 0x12
        let (num, wt, consumed) = read_field_key(&[0x12]).unwrap();
        assert_eq!(num, 2);
        assert_eq!(wt, WireType::LengthDelimited);
        assert_eq!(consumed, 1);
    }

    #[test]
    fn test_varint_max_u64() {
        // u64::MAX = 18446744073709551615
        let data = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x01];
        let (val, consumed) = read_varint(&data).unwrap();
        assert_eq!(val, u64::MAX);
        assert_eq!(consumed, 10);
    }

    #[test]
    fn test_varint_rejects_too_long() {
        let data = [0x80; 11];
        assert!(matches!(read_varint(&data), Err(Error::VarintOverflow)));
    }

    #[test]
    fn test_varint_rejects_invalid_10th_byte_payload() {
        // 10th byte contains data bits beyond bit 63.
        let data = [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x02];
        assert!(matches!(read_varint(&data), Err(Error::VarintOverflow)));
    }

    #[test]
    fn test_field_key_rejects_zero_field_number() {
        // key 0 is invalid in protobuf.
        assert!(read_field_key(&[0x00]).is_err());
    }
}
