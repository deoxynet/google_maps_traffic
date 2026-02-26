use crate::error::{Error, Result};
use crate::types::{Bounds, LatLng};
use std::f64::consts::PI;

const MAX_MERCATOR_LAT: f64 = 85.051_128_78;

/// A tile coordinate in the Web Mercator (XYZ) tiling scheme.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct TileCoord {
    pub zoom: u8,
    pub x: u32,
    pub y: u32,
}

impl TileCoord {
    /// Create a new tile coordinate with validation.
    pub fn new(zoom: u8, x: u32, y: u32) -> Result<Self> {
        if zoom > 21 {
            return Err(Error::InvalidZoom(zoom));
        }
        let max = 1u32 << zoom;
        if x >= max || y >= max {
            return Err(Error::InvalidTileCoord(format!(
                "x={x}, y={y} out of range for zoom={zoom} (max={max})"
            )));
        }
        Ok(Self { zoom, x, y })
    }

    /// Get the tile containing a given lat/lng at the specified zoom level.
    pub fn from_latlng(latlng: &LatLng, zoom: u8) -> Result<Self> {
        if zoom > 21 {
            return Err(Error::InvalidZoom(zoom));
        }
        if !latlng.lat.is_finite() || !latlng.lng.is_finite() {
            return Err(Error::InvalidInput(format!(
                "lat/lng must be finite, got lat={}, lng={}",
                latlng.lat, latlng.lng
            )));
        }
        let n = (1u32 << zoom) as f64;
        let lat = latlng.lat.clamp(-MAX_MERCATOR_LAT, MAX_MERCATOR_LAT);
        let lng = latlng.lng.clamp(-180.0, 180.0);
        let lat_rad = lat.to_radians();

        let x = ((lng + 180.0) / 360.0 * n).floor() as u32;
        let y = ((1.0 - lat_rad.tan().asinh() / PI) / 2.0 * n).floor() as u32;

        let max = 1u32 << zoom;
        Self::new(zoom, x.min(max - 1), y.min(max - 1))
    }

    /// Get all tiles that cover a bounding box at the given zoom level.
    pub fn tiles_for_bounds(bounds: &Bounds, zoom: u8) -> Result<Vec<TileCoord>> {
        let coords = [bounds.south, bounds.west, bounds.north, bounds.east];
        if coords.iter().any(|v| !v.is_finite()) {
            return Err(Error::InvalidInput(
                "bounds must contain finite coordinates".to_string(),
            ));
        }

        let south = bounds.south.min(bounds.north);
        let north = bounds.south.max(bounds.north);
        let sw = Self::from_latlng(&LatLng::new(south, bounds.west), zoom)?;
        let ne = Self::from_latlng(&LatLng::new(north, bounds.east), zoom)?;
        let max = 1u32 << zoom;
        let y_start = ne.y.min(sw.y);
        let y_end = ne.y.max(sw.y);
        let crosses_antimeridian = bounds.west > bounds.east;

        let mut tiles = Vec::new();
        let mut push_x_range = |x_start: u32, x_end: u32| {
            if x_start > x_end {
                return;
            }
            for x in x_start..=x_end {
                for y in y_start..=y_end {
                    tiles.push(TileCoord { zoom, x, y });
                }
            }
        };

        if crosses_antimeridian {
            if max == 1 || sw.x <= ne.x {
                // Wrapped ranges overlap at this zoom; coverage is effectively full-world.
                push_x_range(0, max - 1);
            } else {
                push_x_range(sw.x, max - 1);
                push_x_range(0, ne.x);
            }
        } else {
            push_x_range(sw.x.min(ne.x), sw.x.max(ne.x));
        }
        Ok(tiles)
    }

    /// Convert a pixel coordinate within this tile to a lat/lng.
    /// `px` and `py` are in tile-local coordinates (0..extent).
    pub fn pixel_to_latlng(&self, px: f64, py: f64, extent: f64) -> LatLng {
        let extent = if extent.is_finite() && extent.abs() > f64::EPSILON {
            extent.abs()
        } else {
            256.0
        };
        let px = if px.is_finite() { px } else { 0.0 };
        let py = if py.is_finite() { py } else { 0.0 };
        let n = (1u32 << self.zoom) as f64;
        let lng = wrap_lon((self.x as f64 + px / extent) / n * 360.0 - 180.0);
        let lat_rad = (PI * (1.0 - 2.0 * (self.y as f64 + py / extent) / n))
            .sinh()
            .atan();
        LatLng::new(lat_rad.to_degrees().clamp(-90.0, 90.0), lng)
    }

    /// Get the geographic bounds of this tile.
    pub fn bounds(&self) -> Bounds {
        let nw = self.pixel_to_latlng(0.0, 0.0, 256.0);
        let se = self.pixel_to_latlng(256.0, 256.0, 256.0);
        Bounds::new(se.lat, nw.lng, nw.lat, se.lng)
    }
}

fn wrap_lon(lon: f64) -> f64 {
    if !lon.is_finite() {
        return lon;
    }
    let wrapped = (lon + 180.0).rem_euclid(360.0) - 180.0;
    if wrapped == -180.0 && lon > 0.0 {
        180.0
    } else {
        wrapped
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn test_new_valid() {
        let tc = TileCoord::new(10, 500, 500).unwrap();
        assert_eq!(tc.zoom, 10);
        assert_eq!(tc.x, 500);
    }

    #[test]
    fn test_new_invalid_zoom() {
        assert!(TileCoord::new(22, 0, 0).is_err());
    }

    #[test]
    fn test_new_out_of_range() {
        assert!(TileCoord::new(1, 2, 0).is_err());
    }

    #[test]
    fn test_from_latlng() {
        // NYC ~40.7, -74.0 at zoom 12
        let latlng = LatLng::new(40.7128, -74.0060);
        let tc = TileCoord::from_latlng(&latlng, 12).unwrap();
        assert_eq!(tc.zoom, 12);
        // Known tile coords for NYC at z12
        assert_eq!(tc.x, 1205);
        assert_eq!(tc.y, 1540);
    }

    #[test]
    fn test_tiles_for_bounds() {
        let bounds = Bounds::new(40.70, -74.02, 40.72, -73.98);
        let tiles = TileCoord::tiles_for_bounds(&bounds, 14).unwrap();
        assert!(!tiles.is_empty());
        // All tiles should be at zoom 14
        for t in &tiles {
            assert_eq!(t.zoom, 14);
        }
    }

    #[test]
    fn test_pixel_to_latlng_center() {
        let tc = TileCoord::new(0, 0, 0).unwrap();
        let center = tc.pixel_to_latlng(128.0, 128.0, 256.0);
        assert!((center.lat).abs() < 0.01);
        assert!((center.lng).abs() < 0.01);
    }

    #[test]
    fn test_pixel_to_latlng_wraps_extreme_longitudes() {
        let tc = TileCoord::new(0, 0, 0).unwrap();
        let p = tc.pixel_to_latlng(256.0 * 10_000.0, 128.0, 256.0);
        assert!((-180.0..=180.0).contains(&p.lng));
    }

    #[test]
    fn test_tiles_for_bounds_antimeridian_wraps() {
        let bounds = Bounds::new(-10.0, 170.0, 10.0, -170.0);
        let tiles = TileCoord::tiles_for_bounds(&bounds, 2).unwrap();
        assert_eq!(tiles.len(), 4);
        assert!(tiles.iter().any(|t| t.x == 0));
        assert!(tiles.iter().any(|t| t.x == 3));
    }

    #[test]
    fn test_tiles_for_bounds_normalizes_lat_order() {
        let bounds = Bounds::new(40.72, -74.02, 40.70, -73.98);
        let tiles = TileCoord::tiles_for_bounds(&bounds, 14).unwrap();
        assert!(!tiles.is_empty());
    }

    #[test]
    fn test_from_latlng_clamps_polar_latitudes() {
        let north = TileCoord::from_latlng(&LatLng::new(89.0, 0.0), 10).unwrap();
        let south = TileCoord::from_latlng(&LatLng::new(-89.0, 0.0), 10).unwrap();
        assert_eq!(north.y, 0);
        assert_eq!(south.y, (1u32 << 10) - 1);
    }

    #[test]
    fn test_from_latlng_rejects_non_finite_input() {
        assert!(TileCoord::from_latlng(&LatLng::new(f64::NAN, 0.0), 10).is_err());
        assert!(TileCoord::from_latlng(&LatLng::new(0.0, f64::INFINITY), 10).is_err());
    }

    #[test]
    fn test_tiles_for_bounds_rejects_non_finite_input() {
        let bad = Bounds::new(0.0, f64::NAN, 1.0, 1.0);
        assert!(TileCoord::tiles_for_bounds(&bad, 10).is_err());
    }

    #[test]
    fn test_pixel_to_latlng_sanitizes_non_finite_inputs() {
        let tc = TileCoord::new(2, 1, 1).unwrap();
        let p = tc.pixel_to_latlng(f64::INFINITY, f64::NAN, 0.0);
        assert!(p.lat.is_finite());
        assert!(p.lng.is_finite());
        assert!((-90.0..=90.0).contains(&p.lat));
        assert!((-180.0..=180.0).contains(&p.lng));
    }

    #[test]
    fn test_tiles_for_bounds_antimeridian_wide_span_has_no_duplicates() {
        let bounds = Bounds::new(-10.0, 170.0, 10.0, 160.0);
        let tiles = TileCoord::tiles_for_bounds(&bounds, 2).unwrap();
        let unique: HashSet<_> = tiles.iter().copied().collect();
        assert_eq!(tiles.len(), unique.len());
    }
}
