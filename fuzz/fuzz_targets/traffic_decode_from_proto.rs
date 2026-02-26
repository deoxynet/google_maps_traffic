#![no_main]

use google_maps_traffic::{TrafficFeature, TrafficTile, fuzz_support};
use libfuzzer_sys::fuzz_target;

fn assert_feature_invariants(feature: &TrafficFeature) {
    assert!(feature.bearing.is_finite());

    for point in &feature.geometry {
        assert!(point.lat.is_finite());
        assert!(point.lng.is_finite());
        assert!((-90.0..=90.0).contains(&point.lat));
        assert!((-180.0..=180.0).contains(&point.lng));
    }

    for seg in &feature.segments {
        assert!(seg.start_index < seg.end_index);
        assert!(seg.end_index < feature.geometry.len());
    }
}

fn assert_tile_invariants(tile: &TrafficTile) {
    let max = 1u32 << tile.coord.zoom;
    assert!(tile.coord.zoom <= 21);
    assert!(tile.coord.x < max);
    assert!(tile.coord.y < max);
    for feature in &tile.features {
        assert_feature_invariants(feature);
    }
}

fuzz_target!(|data: &[u8]| {
    if data.len() < 9 {
        return;
    }

    let zoom = data[0] % 22;
    let max = 1u32 << zoom;
    let x_raw = u32::from_le_bytes([data[1], data[2], data[3], data[4]]);
    let y_raw = u32::from_le_bytes([data[5], data[6], data[7], data[8]]);
    let x = x_raw % max;
    let y = y_raw % max;

    if let Ok(tile) = fuzz_support::decode_traffic_tile_from_proto(&data[9..], zoom, x, y) {
        assert_tile_invariants(&tile);
    }
});
