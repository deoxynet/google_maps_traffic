use crate::error::Result;
use crate::geo_utils::compute_bearing;
use crate::geometry;
use crate::protobuf::message::{ProtoMessage, ProtoValue};
use crate::protobuf::wire;
use crate::tile_coord::TileCoord;
use crate::types::*;

pub(crate) const LIVE_TILE_EXTENT: f64 = 4096.0;
pub(crate) const TYPICAL_TILE_EXTENT: f64 = 1024.0;

/// Decode a parsed protobuf message into traffic data for a tile.
pub fn decode_traffic_tile(msg: &ProtoMessage, coord: TileCoord) -> Result<TrafficTile> {
    decode_traffic_tile_with_extent(msg, coord, LIVE_TILE_EXTENT)
}

/// Decode a parsed protobuf message into traffic data for a tile using a
/// caller-provided vector-tile extent.
pub fn decode_traffic_tile_with_extent(
    msg: &ProtoMessage,
    coord: TileCoord,
    tile_extent: f64,
) -> Result<TrafficTile> {
    let mut features = Vec::new();
    let mut incidents = Vec::new();

    // Field 7 is a single message containing repeated f1 sub-messages (features)
    for layer_msg in msg.get_messages(7) {
        for feature_msg in layer_msg.get_messages(1) {
            if let Some(f) = decode_feature(feature_msg, &coord, tile_extent)? {
                features.push(f);
            }
        }
    }

    // Field 10 contains incidents
    for incident_container in msg.get_messages(10) {
        extract_incidents(incident_container, &coord, &mut incidents);
    }

    Ok(TrafficTile {
        coord,
        features,
        incidents,
    })
}

fn decode_feature(
    msg: &ProtoMessage,
    coord: &TileCoord,
    tile_extent: f64,
) -> Result<Option<TrafficFeature>> {
    let layer_type = msg.get_varint(4).unwrap_or(0) as u32;
    let style_flags = msg.get_fixed32(5);

    // Field 1 = geometry bytes (may have been speculatively parsed as a message)
    let geometry_bytes = match get_raw_bytes(msg, 1) {
        Some(b) => b,
        None => return Ok(None),
    };

    let tile_coords = geometry::decode_tile_geometry(&geometry_bytes)?;
    let geom = geometry::tile_coords_to_latlng(&tile_coords, coord, tile_extent);

    if geom.is_empty() {
        return Ok(None);
    }

    // Field 2 = speed segment indices (may also be speculatively parsed)
    let raw_speed_data = get_raw_bytes(msg, 2);

    let segments = match &raw_speed_data {
        Some(data) if !data.is_empty() => decode_speed_segments(data, geom.len(), layer_type),
        _ => {
            // No speed data — whole feature is one segment
            if geom.len() > 1 {
                vec![RoadSegment {
                    start_index: 0,
                    end_index: geom.len() - 1,
                    congestion: congestion_from_layer(layer_type),
                }]
            } else {
                Vec::new()
            }
        }
    };

    let bearing = compute_bearing(&geom);

    Ok(Some(TrafficFeature {
        geometry: geom,
        segments,
        bearing,
        layer_type,
        style_flags,
        raw_speed_data,
    }))
}

/// Decode speed segment point indices.
///
/// Observed payloads behave like *start indices* for independent segment runs:
/// each boundary marks where the next segment begins, and there may be gaps
/// between runs that should not be rendered as connecting edges.
fn decode_speed_segments(data: &[u8], point_count: usize, layer_type: u32) -> Vec<RoadSegment> {
    let congestion = congestion_from_layer(layer_type);
    let mut segments = Vec::new();
    if point_count <= 1 {
        return segments;
    }

    let Some(last_point) = point_count.checked_sub(1) else {
        return segments;
    };

    // Interpret data as varint-encoded boundary point indices.
    let mut boundaries = Vec::new();
    let mut pos = 0usize;
    while pos < data.len() {
        let Ok((raw, consumed)) = wire::read_varint(&data[pos..]) else {
            // Malformed speed index stream: fall back to single-segment coverage.
            return vec![RoadSegment {
                start_index: 0,
                end_index: last_point,
                congestion,
            }];
        };
        pos += consumed;

        let Ok(boundary) = usize::try_from(raw) else {
            continue;
        };
        boundaries.push(boundary);
    }

    // Normalize to monotonic in-range index marks.
    let mut normalized = Vec::with_capacity(boundaries.len() + 1);
    for boundary in boundaries {
        if boundary > last_point {
            continue;
        }

        if normalized
            .last()
            .copied()
            .is_none_or(|prev| boundary > prev)
        {
            normalized.push(boundary);
        }
    }

    if normalized.first() != Some(&0) {
        normalized.insert(0, 0);
    }

    if normalized.last().copied() != Some(point_count) {
        normalized.push(point_count);
    }
    // Start-run interpretation: each mark starts a segment, and the next mark
    // starts a new segment (no implicit bridge edge between runs).
    for starts in normalized.windows(2) {
        let start = starts[0];
        let next_start = starts[1];
        if next_start > start + 1 {
            segments.push(RoadSegment {
                start_index: start,
                end_index: next_start - 1,
                congestion,
            });
        }
    }

    if segments.is_empty() && point_count > 1 {
        segments.push(RoadSegment {
            start_index: 0,
            end_index: last_point,
            congestion,
        });
    }

    segments
}

/// Map layer_type to a congestion level.
///
/// Based on observed patterns:
/// - x81/x82/x83/x84 variants (481-484, 561-564, 721-724)
///   where the last digit may indicate congestion severity.
fn congestion_from_layer(layer_type: u32) -> CongestionLevel {
    match layer_type % 10 {
        1 => CongestionLevel::Normal,
        2 => CongestionLevel::Slow,
        3 => CongestionLevel::Heavy,
        4 => CongestionLevel::Severe,
        _ => CongestionLevel::Unknown(layer_type as u8),
    }
}

/// Recursively search for incident data in field 10's nested structure.
fn extract_incidents(msg: &ProtoMessage, coord: &TileCoord, out: &mut Vec<TrafficIncident>) {
    // Known extension field for incident descriptions
    const INCIDENT_EXT_FIELD: u32 = 43229016;

    for val in msg.get_all(INCIDENT_EXT_FIELD) {
        if let ProtoValue::Message(ext_msg, _) = val
            && let Some(incident) = decode_incident_ext(ext_msg, coord)
        {
            out.push(incident);
        }
    }

    // Recurse into sub-messages (but skip high field numbers — they're extensions)
    for (&field_num, values) in &msg.fields {
        if field_num < 1_000_000 {
            for val in values {
                if let ProtoValue::Message(sub, _) = val {
                    extract_incidents(sub, coord, out);
                }
            }
        }
    }
}

/// Decode an incident from a high-field-number extension message.
/// Structure observed: f1=id, f2=description, f4=type, f6=short_description
fn decode_incident_ext(msg: &ProtoMessage, _coord: &TileCoord) -> Option<TrafficIncident> {
    let description = msg.get_string(2);
    let short_description = msg.get_string(6);
    let incident_type = msg
        .get_varint(4)
        .map(|v| IncidentType::from_value(v as u32))
        .unwrap_or(IncidentType::Other(0));

    if description.is_some() || short_description.is_some() {
        Some(TrafficIncident {
            description,
            short_description,
            location: None,
            incident_type,
        })
    } else {
        None
    }
}

/// Get raw bytes from a field, regardless of whether it was speculatively
/// parsed as a nested message.
fn get_raw_bytes(msg: &ProtoMessage, field: u32) -> Option<Vec<u8>> {
    msg.get_bytes(field).map(|b| b.to_vec())
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

    #[test]
    fn decode_speed_segments_parses_varint_indices_over_127() {
        let mut data = Vec::new();
        for boundary in [2u64, 4, 7, 128] {
            data.extend_from_slice(&encode_varint(boundary));
        }

        let segments = decode_speed_segments(&data, 130, 721);
        let got: Vec<_> = segments
            .iter()
            .map(|s| (s.start_index, s.end_index))
            .collect();
        assert_eq!(got, vec![(0, 1), (2, 3), (4, 6), (7, 127), (128, 129)]);
    }

    #[test]
    fn decode_speed_segments_falls_back_for_malformed_varints() {
        let segments = decode_speed_segments(&[0x80], 10, 721);
        assert_eq!(segments.len(), 1);
        assert_eq!(segments[0].start_index, 0);
        assert_eq!(segments[0].end_index, 9);
    }
}
