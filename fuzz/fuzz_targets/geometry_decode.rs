#![no_main]

use google_maps_traffic::{TileCoord, fuzz_support};
use libfuzzer_sys::fuzz_target;

fn read_u32_le(data: &[u8], offset: usize) -> u32 {
    let mut bytes = [0u8; 4];
    for (i, b) in bytes.iter_mut().enumerate() {
        *b = *data.get(offset + i).unwrap_or(&0);
    }
    u32::from_le_bytes(bytes)
}

fn read_u64_le(data: &[u8], offset: usize) -> u64 {
    let mut bytes = [0u8; 8];
    for (i, b) in bytes.iter_mut().enumerate() {
        *b = *data.get(offset + i).unwrap_or(&0);
    }
    u64::from_le_bytes(bytes)
}

fuzz_target!(|data: &[u8]| {
    let Ok(coords) = fuzz_support::decode_geometry(data) else {
        return;
    };

    let zoom = data.first().copied().unwrap_or(0) % 22;
    let max = 1u32 << zoom;
    let x = read_u32_le(data, 1) % max;
    let y = read_u32_le(data, 5) % max;
    let tile = TileCoord::new(zoom, x, y).expect("tile coordinates are in range");

    let extent_bits = read_u64_le(data, 9);
    let extent = f64::from_bits(extent_bits);

    for (px, py) in coords {
        let point = tile.pixel_to_latlng(px as f64, py as f64, extent);
        assert!(point.lat.is_finite());
        assert!(point.lng.is_finite());
        assert!((-90.0..=90.0).contains(&point.lat));
        assert!((-180.0..=180.0).contains(&point.lng));
    }
});
