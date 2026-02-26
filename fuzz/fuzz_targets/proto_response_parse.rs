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
    if let Ok(tiles) = fuzz_support::parse_proto_response(data) {
        for tile in &tiles {
            assert_tile_invariants(tile);
        }
    }
});
