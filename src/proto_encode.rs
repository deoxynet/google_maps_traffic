//! Minimal protobuf encoder for building `bpb` request parameters.
//!
//! Only encodes the subset of wire types needed for the typical traffic
//! tile request: varints, length-delimited fields, and nested messages.

/// Encode a varint (LEB128) into the buffer.
fn encode_varint(buf: &mut Vec<u8>, mut val: u64) {
    loop {
        let byte = (val & 0x7F) as u8;
        val >>= 7;
        if val == 0 {
            buf.push(byte);
            return;
        }
        buf.push(byte | 0x80);
    }
}

/// Encode a field key (field number + wire type).
fn encode_key(buf: &mut Vec<u8>, field: u32, wire_type: u8) {
    encode_varint(buf, ((field as u64) << 3) | wire_type as u64);
}

/// Encode a varint field.
fn encode_varint_field(buf: &mut Vec<u8>, field: u32, val: u64) {
    encode_key(buf, field, 0);
    encode_varint(buf, val);
}

/// Encode a length-delimited field (bytes/string/nested message).
fn encode_bytes_field(buf: &mut Vec<u8>, field: u32, data: &[u8]) {
    encode_key(buf, field, 2);
    encode_varint(buf, data.len() as u64);
    buf.extend_from_slice(data);
}

/// Encode a string field.
fn encode_string_field(buf: &mut Vec<u8>, field: u32, s: &str) {
    encode_bytes_field(buf, field, s.as_bytes());
}

/// Encode a nested message field. `inner` should be pre-encoded message bytes.
fn encode_message_field(buf: &mut Vec<u8>, field: u32, inner: &[u8]) {
    encode_bytes_field(buf, field, inner);
}

use crate::tile_coord::TileCoord;

/// Build the `bpb` protobuf parameter for the typical traffic proto endpoint.
///
/// `seconds_into_week`: time slot as seconds from Sunday midnight (0 = Sun 00:00,
/// 118800 = Mon 09:00, 464400 = Fri 09:00). Use `DayOfWeek::seconds_into_week(hour)`
/// to compute this. Pass `None` for the server's default (current time's typical).
///
/// The request structure (decoded from observed browser requests):
/// ```text
/// repeated f1 { f1 { f1=zoom, f2=x, f3=y }, f25="" }  // tile coordinates
/// f2 { f1=2, f2="traffic", f3=999999, f4{f1="incidents",f2="1"}, f4{f1="incidents_text",f2="1"} }
/// f3 { f2="en", f3="us", f5=1105, f12{f1=68, f2{f1="set",f2="Roadmap"}}, f20=1 }
/// f4 = 1
/// f6 { render params }
/// f27 { f525000007 { f1{incidents}, f1{incidents_text}, f1{seconds_into_week=VALUE} } }
/// f28 = 768
/// f32 = 1
/// ```
pub fn build_typical_bpb(coords: &[TileCoord], seconds_into_week: Option<u32>) -> Vec<u8> {
    let mut buf = Vec::new();

    // f1 repeated: tile coordinates
    for coord in coords {
        let mut inner_coord = Vec::new();
        encode_varint_field(&mut inner_coord, 1, coord.zoom as u64);
        encode_varint_field(&mut inner_coord, 2, coord.x as u64);
        encode_varint_field(&mut inner_coord, 3, coord.y as u64);

        let mut tile_msg = Vec::new();
        encode_message_field(&mut tile_msg, 1, &inner_coord);
        // f25 = empty string
        encode_string_field(&mut tile_msg, 25, "");

        encode_message_field(&mut buf, 1, &tile_msg);
    }

    // f2: layer config (traffic with incidents)
    {
        let mut f2 = Vec::new();
        encode_varint_field(&mut f2, 1, 2);
        encode_string_field(&mut f2, 2, "traffic");
        encode_varint_field(&mut f2, 3, 999999);
        // f4: incidents param
        {
            let mut f4 = Vec::new();
            encode_string_field(&mut f4, 1, "incidents");
            encode_string_field(&mut f4, 2, "1");
            encode_message_field(&mut f2, 4, &f4);
        }
        // f4: incidents_text param
        {
            let mut f4 = Vec::new();
            encode_string_field(&mut f4, 1, "incidents_text");
            encode_string_field(&mut f4, 2, "1");
            encode_message_field(&mut f2, 4, &f4);
        }
        encode_message_field(&mut buf, 2, &f2);
    }

    // f3: locale
    {
        let mut f3 = Vec::new();
        encode_string_field(&mut f3, 2, "en");
        encode_string_field(&mut f3, 3, "us");
        encode_varint_field(&mut f3, 5, 1105);

        // f12: map type
        {
            let mut f12 = Vec::new();
            encode_varint_field(&mut f12, 1, 68);
            {
                let mut f12_f2 = Vec::new();
                encode_string_field(&mut f12_f2, 1, "set");
                encode_string_field(&mut f12_f2, 2, "Roadmap");
                encode_message_field(&mut f12, 2, &f12_f2);
            }
            encode_message_field(&mut f3, 12, &f12);
        }

        encode_varint_field(&mut f3, 20, 1);
        encode_message_field(&mut buf, 3, &f3);
    }

    // f4 = 1
    encode_varint_field(&mut buf, 4, 1);

    // f6: render parameters
    {
        let mut f6 = Vec::new();
        encode_varint_field(&mut f6, 1, 12);
        encode_varint_field(&mut f6, 2, 2);
        encode_varint_field(&mut f6, 6, 0);
        encode_varint_field(&mut f6, 7, 1);
        encode_varint_field(&mut f6, 8, 2);
        encode_varint_field(&mut f6, 17, 1);
        encode_varint_field(&mut f6, 28, 6);
        encode_varint_field(&mut f6, 32, 1);
        encode_varint_field(&mut f6, 39, 1);
        encode_varint_field(&mut f6, 41, 1);
        encode_varint_field(&mut f6, 43, 1);
        encode_varint_field(&mut f6, 45, 1);
        encode_varint_field(&mut f6, 50, 1);
        encode_varint_field(&mut f6, 60, 1);
        // f67 sub-message
        {
            let mut f67 = Vec::new();
            encode_varint_field(&mut f67, 1, 1);
            encode_varint_field(&mut f67, 2, 1);
            encode_message_field(&mut f6, 67, &f67);
        }
        encode_varint_field(&mut f6, 89, 1);
        encode_message_field(&mut buf, 6, &f6);
    }

    // f27: extension with incidents config
    {
        let mut f27 = Vec::new();
        // Extension field 525000007 (observed in browser requests)
        {
            let mut ext = Vec::new();
            {
                let mut f1 = Vec::new();
                encode_string_field(&mut f1, 1, "incidents");
                encode_string_field(&mut f1, 2, "1");
                encode_message_field(&mut ext, 1, &f1);
            }
            {
                let mut f1 = Vec::new();
                encode_string_field(&mut f1, 1, "incidents_text");
                encode_string_field(&mut f1, 2, "1");
                encode_message_field(&mut ext, 1, &f1);
            }
            if let Some(secs) = seconds_into_week {
                let mut f1 = Vec::new();
                encode_string_field(&mut f1, 1, "seconds_into_week");
                encode_string_field(&mut f1, 2, &secs.to_string());
                encode_message_field(&mut ext, 1, &f1);
            }
            encode_message_field(&mut f27, 525000007, &ext);
        }
        encode_message_field(&mut buf, 27, &f27);
    }

    // f28 = 768
    encode_varint_field(&mut buf, 28, 768);
    // f32 = 1
    encode_varint_field(&mut buf, 32, 1);

    buf
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_encode_varint() {
        let mut buf = Vec::new();
        encode_varint(&mut buf, 0);
        assert_eq!(buf, vec![0x00]);

        buf.clear();
        encode_varint(&mut buf, 150);
        assert_eq!(buf, vec![0x96, 0x01]);

        buf.clear();
        encode_varint(&mut buf, 300);
        assert_eq!(buf, vec![0xAC, 0x02]);
    }

    #[test]
    fn test_build_bpb_roundtrip() {
        use crate::protobuf::ProtoMessage;

        let coords = vec![
            TileCoord::new(14, 2808, 6540).unwrap(),
            TileCoord::new(14, 2809, 6541).unwrap(),
        ];
        let bpb = build_typical_bpb(&coords, Some(464400)); // Friday 9 AM

        // Should parse back as valid protobuf
        let msg = ProtoMessage::parse(&bpb).unwrap();

        // Should have 2 tile coordinate messages in f1
        let tiles = msg.get_messages(1);
        assert_eq!(tiles.len(), 2);

        // First tile: zoom=14, x=2808, y=6540
        let tile0 = tiles[0].get_message(1).unwrap();
        assert_eq!(tile0.get_varint(1), Some(14));
        assert_eq!(tile0.get_varint(2), Some(2808));
        assert_eq!(tile0.get_varint(3), Some(6540));

        // Should have traffic layer in f2
        let f2 = msg.get_messages(2);
        assert_eq!(f2.len(), 1);
        assert_eq!(f2[0].get_string(2), Some("traffic".into()));
    }

    #[test]
    fn test_bpb_without_time() {
        use crate::protobuf::ProtoMessage;

        let coords = vec![TileCoord::new(14, 2808, 6540).unwrap()];
        let bpb = build_typical_bpb(&coords, None);

        // Should still parse as valid protobuf
        let msg = ProtoMessage::parse(&bpb).unwrap();
        assert_eq!(msg.get_messages(1).len(), 1);
    }
}
