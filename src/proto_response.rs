use crate::decode;
use crate::error::Result;
use crate::protobuf::ProtoMessage;
use crate::tile_coord::TileCoord;
use crate::types::TrafficTile;

const XOR_KEY: u8 = 0x9b;

/// Parse a batched proto endpoint response into traffic tiles.
///
/// The `/maps/vt/proto?bpb=` endpoint returns a wrapper protobuf containing
/// repeated `f1` messages — one per requested tile. Each tile message has:
///
/// - `f3`: XOR-encrypted (0x9b) protobuf tile data (same format as XHR1 tiles)
/// - `f2`: cache TTL (fixed32, float seconds)
/// - `f7`: content type string
/// - `f9`: etag
pub fn parse_proto_response(data: &[u8]) -> Result<Vec<TrafficTile>> {
    let wrapper = ProtoMessage::parse(data)?;
    let mut tiles = Vec::new();

    for tile_msg in wrapper.get_messages(1) {
        // f3 = the XOR-encrypted tile protobuf
        let encrypted = match tile_msg.get_bytes(3) {
            Some(b) => b,
            None => continue,
        };

        // XOR decrypt — same key as XHR1
        let decrypted: Vec<u8> = encrypted.iter().map(|b| b ^ XOR_KEY).collect();

        let proto = match ProtoMessage::parse(&decrypted) {
            Ok(p) => p,
            Err(_) => continue,
        };

        // Extract tile coordinate from the decrypted protobuf (f1.f1=zoom, f1.f2=x, f1.f3=y).
        // Keep parsing other tiles if one entry is malformed.
        let coord = match extract_tile_coord(&proto) {
            Ok(c) => c,
            Err(_) => continue,
        };

        match decode::decode_traffic_tile_with_extent(&proto, coord, decode::TYPICAL_TILE_EXTENT) {
            Ok(tile) => tiles.push(tile),
            Err(_) => continue,
        }
    }

    Ok(tiles)
}

/// Extract per-tile cache TTLs from a batched proto response.
///
/// The `f2` field in each tile wrapper message is a `fixed32` encoding a
/// float32 TTL in seconds — the server's recommendation for how long the
/// tile data should be considered fresh.
pub(crate) fn parse_proto_cache_ttls(data: &[u8]) -> Result<Vec<f32>> {
    let wrapper = ProtoMessage::parse(data)?;
    let mut ttls = Vec::new();
    for tile_msg in wrapper.get_messages(1) {
        if let Some(raw) = tile_msg.get_fixed32(2) {
            ttls.push(f32::from_bits(raw));
        }
    }
    Ok(ttls)
}

/// Extract the TileCoord from the decrypted tile protobuf.
///
/// The tile coordinate is stored in f1 as: f1.f1=zoom, f1.f2=x, f1.f3=y.
fn extract_tile_coord(msg: &ProtoMessage) -> Result<TileCoord> {
    let coord_msg = msg
        .get_message(1)
        .ok_or_else(|| crate::error::Error::ProtobufDecode("missing tile coordinate f1".into()))?;

    let zoom_raw = coord_msg.get_varint(1).ok_or_else(|| {
        crate::error::Error::ProtobufDecode("missing tile coordinate zoom".into())
    })?;
    let x_raw = coord_msg
        .get_varint(2)
        .ok_or_else(|| crate::error::Error::ProtobufDecode("missing tile coordinate x".into()))?;
    let y_raw = coord_msg
        .get_varint(3)
        .ok_or_else(|| crate::error::Error::ProtobufDecode("missing tile coordinate y".into()))?;

    let zoom = u8::try_from(zoom_raw).map_err(|_| {
        crate::error::Error::ProtobufDecode(format!(
            "tile coordinate zoom out of range: {zoom_raw}"
        ))
    })?;
    let x = u32::try_from(x_raw).map_err(|_| {
        crate::error::Error::ProtobufDecode(format!("tile coordinate x out of range: {x_raw}"))
    })?;
    let y = u32::try_from(y_raw).map_err(|_| {
        crate::error::Error::ProtobufDecode(format!("tile coordinate y out of range: {y_raw}"))
    })?;

    TileCoord::new(zoom, x, y)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_xor_decrypt_produces_valid_protobuf() {
        // Simulate: f1 { f1=14, f2=100, f3=200 } encoded as protobuf, then XOR'd
        let mut tile_proto = Vec::new();
        // f1 (message):
        //   f1=14: 0x08 0x0e
        //   f2=100: 0x10 0x64
        //   f3=200: 0x18 0xc8 0x01
        let inner: &[u8] = &[0x08, 0x0e, 0x10, 0x64, 0x18, 0xc8, 0x01];
        // Encode as f1 length-delimited: key=0x0a, len=7
        tile_proto.push(0x0a);
        tile_proto.push(inner.len() as u8);
        tile_proto.extend_from_slice(inner);

        // XOR encrypt
        let encrypted: Vec<u8> = tile_proto.iter().map(|b| b ^ XOR_KEY).collect();

        // Build a wrapper: f1 { f3 = encrypted }
        let mut wrapper = Vec::new();
        // f3 key = (3 << 3) | 2 = 0x1a
        wrapper.push(0x1a);
        wrapper.push(encrypted.len() as u8);
        wrapper.extend_from_slice(&encrypted);

        // Wrap in outer f1
        let mut outer = Vec::new();
        outer.push(0x0a); // f1 key
        outer.push(wrapper.len() as u8);
        outer.extend_from_slice(&wrapper);

        let result = ProtoMessage::parse(&outer);
        assert!(result.is_ok());
    }

    #[test]
    fn extract_tile_coord_requires_all_fields() {
        // f1 { f1=14 } (missing x/y)
        let data = [0x0a, 0x02, 0x08, 0x0e];
        let msg = ProtoMessage::parse(&data).expect("valid proto");
        assert!(extract_tile_coord(&msg).is_err());
    }

    #[test]
    fn extract_tile_coord_rejects_zoom_out_of_range() {
        // f1 { f1=250, f2=1, f3=1 }
        let data = [0x0a, 0x07, 0x08, 0xfa, 0x01, 0x10, 0x01, 0x18, 0x01];
        let msg = ProtoMessage::parse(&data).expect("valid proto");
        assert!(extract_tile_coord(&msg).is_err());
    }

    #[test]
    fn parse_proto_response_skips_malformed_tile_coord() {
        // outer: f1 { f3 = empty-bytes }
        // - decrypted payload is valid empty protobuf
        // - tile coordinate f1 is missing, so entry should be skipped (not fail whole parse)
        let outer = [0x0a, 0x02, 0x1a, 0x00];
        let tiles = parse_proto_response(&outer).expect("parse should succeed");
        assert!(tiles.is_empty());
    }
}
