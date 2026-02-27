use crate::error::{Error, Result};
use crate::protobuf::wire;
use crate::tile_coord::TileCoord;
use crate::types::LatLng;

const CMD_MOVE_TO: u64 = 1;
const CMD_LINE_TO: u64 = 2;
const CMD_CLOSE_PATH: u64 = 7;

#[derive(Debug, Clone, Default)]
pub(crate) struct DecodedGeometry {
    pub coords: Vec<(i32, i32)>,
    /// Indices in `coords` where a new MoveTo subpath begins.
    pub move_to_indices: Vec<usize>,
}

/// Decode delta-encoded zigzag varint pairs from raw bytes into tile-local coordinates.
///
/// The encoding stores (dx, dy) pairs as zigzag-encoded varints.
/// Each coordinate is the cumulative sum of deltas.
#[allow(dead_code)]
pub fn decode_tile_geometry(data: &[u8]) -> Result<Vec<(i32, i32)>> {
    Ok(decode_tile_geometry_with_path_breaks(data)?.coords)
}

pub(crate) fn decode_tile_geometry_with_path_breaks(data: &[u8]) -> Result<DecodedGeometry> {
    if data.is_empty() {
        return Ok(DecodedGeometry::default());
    }

    // Preferred path: protobuf vector-tile command stream (MoveTo/LineTo/ClosePath).
    // Keep a compatibility fallback for plain (dx,dy) pair payloads observed in older captures.
    if let Ok(decoded) = decode_command_geometry(data)
        && !decoded.coords.is_empty()
    {
        return Ok(decoded);
    }

    let coords = decode_delta_pairs_geometry(data)?;
    let move_to_indices = if coords.is_empty() {
        Vec::new()
    } else {
        vec![0]
    };
    Ok(DecodedGeometry {
        coords,
        move_to_indices,
    })
}

fn decode_command_geometry(data: &[u8]) -> Result<DecodedGeometry> {
    let mut coords = Vec::new();
    let mut move_to_indices = Vec::new();
    let mut pos = 0;
    let mut cx: i32 = 0;
    let mut cy: i32 = 0;

    let mut cmd_id: u64 = 0;
    let mut cmd_remaining: u32 = 0;

    while pos < data.len() {
        if cmd_remaining == 0 {
            let cmd_pos = pos;
            let (cmd, consumed) = wire::read_varint(&data[pos..]).map_err(|e| {
                Error::GeometryDecode(format!("command varint at offset {cmd_pos}: {e}"))
            })?;
            pos += consumed;

            let id = cmd & 0x7;
            let count = (cmd >> 3) as u32;
            if count == 0 {
                return Err(Error::GeometryDecode(format!(
                    "zero command count at offset {cmd_pos}"
                )));
            }
            match id {
                CMD_MOVE_TO | CMD_LINE_TO | CMD_CLOSE_PATH => {}
                _ => {
                    return Err(Error::GeometryDecode(format!(
                        "unknown command id {id} at offset {cmd_pos}"
                    )));
                }
            }
            cmd_id = id;
            cmd_remaining = count;
        }

        match cmd_id {
            CMD_MOVE_TO | CMD_LINE_TO => {
                let x_pos = pos;
                let (vx, consumed) = wire::read_varint(&data[pos..]).map_err(|e| {
                    Error::GeometryDecode(format!("x varint at offset {x_pos}: {e}"))
                })?;
                pos += consumed;

                if pos >= data.len() {
                    return Err(Error::GeometryDecode(format!(
                        "missing y varint after x at offset {x_pos}"
                    )));
                }

                let y_pos = pos;
                let (vy, consumed) = wire::read_varint(&data[pos..]).map_err(|e| {
                    Error::GeometryDecode(format!("y varint at offset {y_pos}: {e}"))
                })?;
                pos += consumed;

                let dx = i32::try_from(wire::zigzag_decode(vx)).map_err(|_| {
                    Error::GeometryDecode(format!(
                        "x delta out of i32 range at offset {x_pos}: {vx}"
                    ))
                })?;
                let dy = i32::try_from(wire::zigzag_decode(vy)).map_err(|_| {
                    Error::GeometryDecode(format!(
                        "y delta out of i32 range at offset {y_pos}: {vy}"
                    ))
                })?;

                cx = cx
                    .checked_add(dx)
                    .ok_or_else(|| Error::GeometryDecode("x coordinate overflow".into()))?;
                cy = cy
                    .checked_add(dy)
                    .ok_or_else(|| Error::GeometryDecode("y coordinate overflow".into()))?;

                coords.push((cx, cy));
                if cmd_id == CMD_MOVE_TO {
                    move_to_indices.push(coords.len() - 1);
                }
                cmd_remaining -= 1;
            }
            CMD_CLOSE_PATH => {
                // For traffic polylines, closed rings are not expected.
                // Consume the command without emitting points.
                cmd_remaining -= 1;
            }
            _ => unreachable!(),
        }
    }

    if cmd_remaining != 0 {
        return Err(Error::GeometryDecode(
            "truncated command sequence in geometry data".into(),
        ));
    }

    if !coords.is_empty() && move_to_indices.first().copied() != Some(0) {
        move_to_indices.insert(0, 0);
    }

    Ok(DecodedGeometry {
        coords,
        move_to_indices,
    })
}

fn decode_delta_pairs_geometry(data: &[u8]) -> Result<Vec<(i32, i32)>> {
    let mut coords = Vec::new();
    let mut pos = 0;
    let mut cx: i32 = 0;
    let mut cy: i32 = 0;

    while pos < data.len() {
        let x_pos = pos;
        let (vx, consumed) = wire::read_varint(&data[pos..])
            .map_err(|e| Error::GeometryDecode(format!("x varint at offset {x_pos}: {e}")))?;
        pos += consumed;

        if pos >= data.len() {
            return Err(Error::GeometryDecode(
                "odd number of varints in geometry data".into(),
            ));
        }

        let y_pos = pos;
        let (vy, consumed) = wire::read_varint(&data[pos..])
            .map_err(|e| Error::GeometryDecode(format!("y varint at offset {y_pos}: {e}")))?;
        pos += consumed;

        let dx = i32::try_from(wire::zigzag_decode(vx)).map_err(|_| {
            Error::GeometryDecode(format!("x delta out of i32 range at offset {x_pos}: {vx}"))
        })?;
        let dy = i32::try_from(wire::zigzag_decode(vy)).map_err(|_| {
            Error::GeometryDecode(format!("y delta out of i32 range at offset {y_pos}: {vy}"))
        })?;

        cx = cx
            .checked_add(dx)
            .ok_or_else(|| Error::GeometryDecode("x coordinate overflow".into()))?;
        cy = cy
            .checked_add(dy)
            .ok_or_else(|| Error::GeometryDecode("y coordinate overflow".into()))?;

        coords.push((cx, cy));
    }

    Ok(coords)
}

/// Convert tile-local coordinates to geographic lat/lng using the tile's position.
pub fn tile_coords_to_latlng(coords: &[(i32, i32)], tile: &TileCoord, extent: f64) -> Vec<LatLng> {
    coords
        .iter()
        .map(|&(px, py)| tile.pixel_to_latlng(px as f64, py as f64, extent))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

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

    fn zigzag_encode(v: i64) -> u64 {
        ((v << 1) ^ (v >> 63)) as u64
    }

    #[test]
    fn test_decode_single_point() {
        // Encode (10, 20) as zigzag varints
        // zigzag(10) = 20 = 0x14, zigzag(20) = 40 = 0x28
        let data = [0x14, 0x28];
        let coords = decode_tile_geometry(&data).unwrap();
        assert_eq!(coords, vec![(10, 20)]);
    }

    #[test]
    fn test_decode_delta() {
        // First point: (10, 20), delta (5, -3)
        // zigzag(10)=20=0x14, zigzag(20)=40=0x28
        // zigzag(5)=10=0x0A, zigzag(-3)=5=0x05
        let data = [0x14, 0x28, 0x0A, 0x05];
        let coords = decode_tile_geometry(&data).unwrap();
        assert_eq!(coords, vec![(10, 20), (15, 17)]);
    }

    #[test]
    fn test_tile_coords_to_latlng() {
        let tile = TileCoord::new(0, 0, 0).unwrap();
        let coords = vec![(2048, 2048)];
        let latllngs = tile_coords_to_latlng(&coords, &tile, 4096.0);
        assert_eq!(latllngs.len(), 1);
        // Center of the world tile
        assert!((latllngs[0].lat).abs() < 0.01);
        assert!((latllngs[0].lng).abs() < 0.01);
    }

    #[test]
    fn test_decode_rejects_delta_out_of_i32_range() {
        let dx = (i32::MAX as i64) + 1;
        let mut data = Vec::new();
        data.extend_from_slice(&encode_varint(zigzag_encode(dx)));
        data.extend_from_slice(&encode_varint(zigzag_encode(0)));
        assert!(decode_tile_geometry(&data).is_err());
    }

    #[test]
    fn test_decode_rejects_coordinate_overflow() {
        let mut data = Vec::new();
        // First point moves to i32::MAX, second point adds +1 (overflow).
        data.extend_from_slice(&encode_varint(zigzag_encode(i32::MAX as i64)));
        data.extend_from_slice(&encode_varint(zigzag_encode(0)));
        data.extend_from_slice(&encode_varint(zigzag_encode(1)));
        data.extend_from_slice(&encode_varint(zigzag_encode(0)));
        assert!(decode_tile_geometry(&data).is_err());
    }

    #[test]
    fn test_decode_command_stream_move_and_line_to() {
        // MoveTo count=1 => (1 | (1 << 3)) = 9
        // LineTo count=2 => (2 | (2 << 3)) = 18
        // Points: (5,7), (8,9), (10,12)
        let data = [
            9,  // MoveTo 1
            10, // dx=+5
            14, // dy=+7
            18, // LineTo 2
            6,  // dx=+3
            4,  // dy=+2
            4,  // dx=+2
            6,  // dy=+3
        ];
        let coords = decode_tile_geometry(&data).unwrap();
        assert_eq!(coords, vec![(5, 7), (8, 9), (10, 12)]);
    }

    #[test]
    fn test_decode_command_stream_ignores_closepath() {
        // MoveTo(1) => (1,1), ClosePath(1), LineTo(1) => (2,1)
        let data = [9, 2, 2, 15, 10, 2, 0];
        let coords = decode_tile_geometry(&data).unwrap();
        assert_eq!(coords, vec![(1, 1), (2, 1)]);
    }

    #[test]
    fn test_decode_tracks_move_to_path_breaks() {
        // MoveTo(1): (5,5), LineTo(1): (6,5), MoveTo(1): (10,10), LineTo(1): (11,10)
        let data = [
            9, 10, 10, // MoveTo 1
            10, 2, 0, // LineTo 1
            9, 8, 10, // MoveTo 1
            10, 2, 0, // LineTo 1
        ];
        let decoded = decode_tile_geometry_with_path_breaks(&data).unwrap();
        assert_eq!(decoded.coords, vec![(5, 5), (6, 5), (10, 10), (11, 10)]);
        assert_eq!(decoded.move_to_indices, vec![0, 2]);
    }
}
