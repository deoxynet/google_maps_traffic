#![no_main]

use google_maps_traffic::{
    CongestionLevel, EdgeTravelDirection, LatLng, MatchConfig, MaxSpeedSource, Oneway, OsmEdge,
    RoadSegment, SpeedEstimationConfig, TileCoord, TrafficFeature, TrafficTile, fuzz_support,
};
use libfuzzer_sys::fuzz_target;
use std::collections::HashSet;

struct Cursor<'a> {
    data: &'a [u8],
    pos: usize,
}

impl<'a> Cursor<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, pos: 0 }
    }

    fn next_u8(&mut self) -> u8 {
        let b = *self.data.get(self.pos).unwrap_or(&0);
        self.pos = self.pos.saturating_add(1);
        b
    }

    fn next_u16(&mut self) -> u16 {
        let lo = self.next_u8();
        let hi = self.next_u8();
        u16::from_le_bytes([lo, hi])
    }

    fn next_u32(&mut self) -> u32 {
        let b0 = self.next_u8();
        let b1 = self.next_u8();
        let b2 = self.next_u8();
        let b3 = self.next_u8();
        u32::from_le_bytes([b0, b1, b2, b3])
    }

    fn next_i16(&mut self) -> i16 {
        let lo = self.next_u8();
        let hi = self.next_u8();
        i16::from_le_bytes([lo, hi])
    }
}

fn wrap_lon(lon: f64) -> f64 {
    let wrapped = (lon + 180.0).rem_euclid(360.0) - 180.0;
    if wrapped == -180.0 && lon > 0.0 {
        180.0
    } else {
        wrapped
    }
}

fn map_lat(raw: i16) -> f64 {
    let v = raw as f64 / i16::MAX as f64;
    (v * 85.0).clamp(-85.0, 85.0)
}

fn map_lng(raw: i16) -> f64 {
    let v = raw as f64 / i16::MAX as f64;
    (v * 180.0).clamp(-180.0, 180.0)
}

fn map_congestion(v: u8) -> CongestionLevel {
    match v % 5 {
        0 => CongestionLevel::Normal,
        1 => CongestionLevel::Slow,
        2 => CongestionLevel::Heavy,
        3 => CongestionLevel::Severe,
        _ => CongestionLevel::Unknown(v),
    }
}

fuzz_target!(|data: &[u8]| {
    let mut c = Cursor::new(data);

    let edge_count = (c.next_u8() % 20) as usize;
    let mut edges = Vec::with_capacity(edge_count);
    let highways = [
        "motorway",
        "primary",
        "secondary",
        "tertiary",
        "residential",
        "service",
    ];

    for i in 0..edge_count {
        let start_lat = map_lat(c.next_i16());
        let start_lng = map_lng(c.next_i16());

        let dlat = (c.next_i16() as f64 / i16::MAX as f64) * 0.3;
        let dlng = (c.next_i16() as f64 / i16::MAX as f64) * 0.3;
        let end_lat = (start_lat + dlat).clamp(-85.0, 85.0);
        let end_lng = wrap_lon(start_lng + dlng);

        let oneway = match c.next_u8() % 3 {
            0 => Oneway::No,
            1 => Oneway::Forward,
            _ => Oneway::Reverse,
        };

        let forward_speed = 5.0 + (c.next_u16() as f64 / u16::MAX as f64) * 145.0;
        let backward_speed = 5.0 + (c.next_u16() as f64 / u16::MAX as f64) * 145.0;
        let nominal = match oneway {
            Oneway::Forward => forward_speed,
            Oneway::Reverse => backward_speed,
            Oneway::No => (forward_speed + backward_speed) * 0.5,
        };

        let source = match c.next_u8() % 4 {
            0 => MaxSpeedSource::TagKph,
            1 => MaxSpeedSource::TagMph,
            2 => MaxSpeedSource::ZoneTag,
            _ => MaxSpeedSource::HighwayDefault,
        };

        edges.push(OsmEdge {
            way_id: i as i64 + 1,
            start_node_id: i as i64 * 2 + 100,
            end_node_id: i as i64 * 2 + 101,
            start: LatLng::new(start_lat, start_lng),
            end: LatLng::new(end_lat, end_lng),
            bearing: (c.next_u16() as f64 / u16::MAX as f64) * 360.0,
            highway: highways[(c.next_u8() as usize) % highways.len()].to_string(),
            oneway,
            max_speed_kph: nominal,
            max_speed_source: source,
            max_speed_forward_kph: forward_speed,
            max_speed_forward_source: source,
            max_speed_backward_kph: backward_speed,
            max_speed_backward_source: source,
        });
    }

    let zoom = c.next_u8() % 22;
    let max = 1u32 << zoom;
    let x = c.next_u32() % max;
    let y = c.next_u32() % max;
    let coord = TileCoord::new(zoom, x, y).expect("x/y normalized to zoom bounds");

    let feature_count = (c.next_u8() % 12) as usize;
    let mut features = Vec::with_capacity(feature_count);
    for _ in 0..feature_count {
        let point_count = (c.next_u8() % 20) as usize;
        let mut geometry = Vec::with_capacity(point_count);
        let mut lat = map_lat(c.next_i16());
        let mut lng = map_lng(c.next_i16());
        for _ in 0..point_count {
            lat = (lat + (c.next_i16() as f64 / i16::MAX as f64) * 0.02).clamp(-85.0, 85.0);
            lng = wrap_lon(lng + (c.next_i16() as f64 / i16::MAX as f64) * 0.02);
            geometry.push(LatLng::new(lat, lng));
        }

        let mut segments = Vec::new();
        if point_count >= 2 {
            let desired = (c.next_u8() % 6) as usize + 1;
            let mut idx = 0usize;
            for _ in 0..desired {
                if idx >= point_count - 1 {
                    break;
                }
                let mode = c.next_u8() % 4;
                let (start_index, end_index) = if mode == 0 {
                    (
                        (c.next_u8() as usize) % point_count,
                        (c.next_u8() as usize) % point_count,
                    )
                } else {
                    let remaining = (point_count - 1).saturating_sub(idx).max(1);
                    let step = 1 + ((c.next_u8() as usize) % remaining);
                    let start = idx;
                    let end = (idx + step).min(point_count - 1);
                    idx = end;
                    (start, end)
                };
                segments.push(RoadSegment {
                    start_index,
                    end_index,
                    congestion: map_congestion(c.next_u8()),
                });
            }
            if segments.is_empty() {
                segments.push(RoadSegment {
                    start_index: 0,
                    end_index: point_count - 1,
                    congestion: CongestionLevel::Normal,
                });
            }
        }

        features.push(TrafficFeature {
            geometry,
            segments,
            bearing: (c.next_u16() as f64 / u16::MAX as f64) * 360.0,
            layer_type: 400 + (c.next_u16() as u32 % 400),
            style_flags: if c.next_u8() & 1 == 0 {
                Some(c.next_u32())
            } else {
                None
            },
            raw_speed_data: None,
        });
    }

    let tile = TrafficTile {
        coord,
        features,
        incidents: Vec::new(),
    };

    let mut max_distance_m = (c.next_u16() as f64 / u16::MAX as f64) * 300.0;
    let mut max_bearing_diff_deg = (c.next_u16() as f64 / u16::MAX as f64) * 180.0;
    let mut distance_weight = c.next_u8() as f64 / u8::MAX as f64;
    let mut bearing_weight = c.next_u8() as f64 / u8::MAX as f64;

    // Inject occasional non-finite config values to harden matcher input handling.
    if c.next_u8() % 64 == 0 {
        max_distance_m = f64::NAN;
    }
    if c.next_u8() % 64 == 0 {
        max_bearing_diff_deg = f64::NAN;
    }
    if c.next_u8() % 64 == 0 {
        distance_weight = f64::NAN;
    }
    if c.next_u8() % 64 == 0 {
        bearing_weight = f64::NAN;
    }

    let config = MatchConfig {
        max_distance_m,
        max_bearing_diff_deg,
        distance_weight,
        bearing_weight,
    };

    let output = fuzz_support::match_osm_tile(&edges, &tile, &config);

    let mut prev_score = f64::NEG_INFINITY;
    let mut seen = HashSet::new();
    for m in &output.matches {
        assert!(m.distance_m.is_finite());
        assert!(m.distance_m >= 0.0);
        assert!(m.bearing_diff_deg.is_finite());
        assert!((0.0..=180.0).contains(&m.bearing_diff_deg));
        assert!(m.score.is_finite());
        assert!(m.score >= prev_score);
        prev_score = m.score;

        let edge_key = (m.edge.way_id, m.edge.start_node_id, m.edge.end_node_id);
        assert!(seen.insert(edge_key));

        assert!(m.edge.start.lat.is_finite());
        assert!(m.edge.start.lng.is_finite());
        assert!(m.edge.end.lat.is_finite());
        assert!(m.edge.end.lng.is_finite());
        assert!((-90.0..=90.0).contains(&m.edge.start.lat));
        assert!((-180.0..=180.0).contains(&m.edge.start.lng));
        assert!((-90.0..=90.0).contains(&m.edge.end.lat));
        assert!((-180.0..=180.0).contains(&m.edge.end.lng));
    }

    for u in &output.unmatched {
        assert!(u.midpoint.lat.is_finite());
        assert!(u.midpoint.lng.is_finite());
        assert!(u.bearing.is_finite());
        assert!((-90.0..=90.0).contains(&u.midpoint.lat));
        assert!((-180.0..=180.0).contains(&u.midpoint.lng));
    }

    let speed_cfg = SpeedEstimationConfig::default();
    for m in &output.matches {
        let forward =
            m.estimate_speed_directional_kph(EdgeTravelDirection::Forward, Some(12), &speed_cfg);
        let reverse =
            m.estimate_speed_directional_kph(EdgeTravelDirection::Reverse, Some(12), &speed_cfg);

        match m.edge.oneway {
            Oneway::Forward => assert!(reverse.is_none()),
            Oneway::Reverse => assert!(forward.is_none()),
            Oneway::No => {}
        }
        if let Some(v) = forward {
            assert!(v.is_finite());
            assert!(v >= 0.0);
        }
        if let Some(v) = reverse {
            assert!(v.is_finite());
            assert!(v >= 0.0);
        }
    }
});
