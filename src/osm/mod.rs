mod index;
mod load;
mod matcher;
mod types;

use crate::client::TrafficClient;
use crate::error::Error;
use crate::tile_coord::TileCoord;
use crate::types::{Bounds, DayOfWeek, LatLng, TrafficTile};
use index::{IndexedEdge, build_rtree};
use rstar::RTree;
use std::path::Path;
pub use types::{
    EdgeTravelDirection, MatchConfig, MatchOutput, MatchResult, MaxSpeedSource, Oneway, OsmEdge,
    SpeedEstimationConfig, TypicalCalibrationSpec, UnmatchedSegment,
};

#[derive(Debug, thiserror::Error)]
pub enum OsmError {
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),
    #[error("OSM PBF parse error: {0}")]
    Pbf(String),
    #[error("invalid input: {0}")]
    InvalidInput(String),
    #[error("no routable OSM edges were loaded")]
    EmptyNetwork,
}

pub struct OsmRoadNetwork {
    edges: Vec<OsmEdge>,
    tree: RTree<IndexedEdge>,
}

const TRAFFIC_TILE_BUFFER_M: f64 = 500.0;
const FULL_WORLD_LON_EPS: f64 = 1e-9;

impl OsmRoadNetwork {
    pub fn from_pbf(path: impl AsRef<Path>) -> Result<Self, OsmError> {
        let edges = load::load_edges_from_pbf(path.as_ref())?;
        Self::from_edges(edges)
    }

    pub fn from_pbf_bounded(path: impl AsRef<Path>, bounds: &Bounds) -> Result<Self, OsmError> {
        let edges = load::load_edges_from_pbf_bounded(path.as_ref(), bounds)?;
        Self::from_edges(edges)
    }

    /// Build a bounded network sized to full tile coverage for a traffic query.
    ///
    /// `TrafficClient::get_traffic(bounds, zoom)` fetches whole tiles, not clipped
    /// geometries. This helper expands `bounds` to the union of those tile bounds
    /// plus a small safety margin for vector-tile geometry buffers.
    pub fn from_pbf_for_traffic_bounds(
        path: impl AsRef<Path>,
        bounds: &Bounds,
        zoom: u8,
    ) -> Result<Self, OsmError> {
        let tile_bounds = tile_coverage_bounds(bounds, zoom, TRAFFIC_TILE_BUFFER_M)?;
        Self::from_pbf_bounded(path, &tile_bounds)
    }

    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    pub fn edges(&self) -> &[OsmEdge] {
        &self.edges
    }

    pub fn match_tile(&self, tile: &TrafficTile, config: &MatchConfig) -> MatchOutput {
        matcher::match_tile(&self.edges, &self.tree, tile, config)
    }

    pub fn match_tiles(&self, tiles: &[TrafficTile], config: &MatchConfig) -> MatchOutput {
        matcher::match_tiles(&self.edges, &self.tree, tiles, config)
    }

    /// Calibrate hourly speed factors from Google's typical traffic for a day.
    ///
    /// For each requested hour, fetches typical traffic, matches it to OSM edges,
    /// and derives a background hour factor from the matched congestion mix.
    pub async fn calibrate_hourly_factors_from_typical(
        &self,
        client: &TrafficClient,
        spec: &TypicalCalibrationSpec<'_>,
    ) -> crate::error::Result<SpeedEstimationConfig> {
        let mut calibrated = spec.base.clone();

        for &hour in spec.hours {
            if hour > 23 {
                return Err(Error::InvalidInput(format!(
                    "hour must be in 0..=23, got {hour}"
                )));
            }
            let tiles = client
                .get_typical_traffic(spec.bounds, spec.zoom, spec.day, hour)
                .await?;
            let matched = self.match_tiles(&tiles, spec.match_config);
            if let Some(factor) = spec
                .base
                .calibrated_hour_factor_from_matches(&matched.matches)
            {
                calibrated.set_hour_factor(hour, factor);
            }
        }

        Ok(calibrated)
    }

    /// Convenience helper that calibrates all 24 hours for a day.
    pub async fn calibrate_full_day_from_typical(
        &self,
        client: &TrafficClient,
        bounds: &Bounds,
        zoom: u8,
        day: DayOfWeek,
        match_config: &MatchConfig,
        base: &SpeedEstimationConfig,
    ) -> crate::error::Result<SpeedEstimationConfig> {
        let hours: Vec<u8> = (0..24).collect();
        self.calibrate_hourly_factors_from_typical(
            client,
            &TypicalCalibrationSpec {
                bounds,
                zoom,
                day,
                hours: &hours,
                match_config,
                base,
            },
        )
        .await
    }

    fn from_edges(edges: Vec<OsmEdge>) -> Result<Self, OsmError> {
        if edges.is_empty() {
            return Err(OsmError::EmptyNetwork);
        }
        let tree = build_rtree(&edges);
        Ok(Self { edges, tree })
    }
}

#[cfg(feature = "fuzzing")]
pub(crate) fn fuzz_match_tile_from_edges(
    edges: &[OsmEdge],
    tile: &TrafficTile,
    config: &MatchConfig,
) -> MatchOutput {
    let tree = build_rtree(edges);
    matcher::match_tile(edges, &tree, tile, config)
}

fn tile_coverage_bounds(bounds: &Bounds, zoom: u8, pad_m: f64) -> Result<Bounds, OsmError> {
    let tiles = TileCoord::tiles_for_bounds(bounds, zoom)
        .map_err(|e| OsmError::InvalidInput(e.to_string()))?;

    if tiles.is_empty() {
        return Err(OsmError::InvalidInput(
            "no tiles generated for provided bounds".to_string(),
        ));
    }

    let mut south = f64::INFINITY;
    let mut north = f64::NEG_INFINITY;
    let mut west = f64::INFINITY;
    let mut east = f64::NEG_INFINITY;

    for tile in tiles {
        let b = tile.bounds();
        south = south.min(b.south);
        north = north.max(b.north);
        west = west.min(b.west);
        east = east.max(b.east);
    }

    if bounds.west > bounds.east {
        let south_input = bounds.south.min(bounds.north);
        let north_input = bounds.south.max(bounds.north);
        let sw = TileCoord::from_latlng(&LatLng::new(south_input, bounds.west), zoom)
            .map_err(|e| OsmError::InvalidInput(e.to_string()))?;
        let ne = TileCoord::from_latlng(&LatLng::new(north_input, bounds.east), zoom)
            .map_err(|e| OsmError::InvalidInput(e.to_string()))?;

        let columns = 1u32 << zoom;
        let covered_columns = (columns - sw.x) as u64 + (ne.x as u64 + 1);

        if covered_columns >= columns as u64 {
            west = -180.0;
            east = 180.0;
        } else {
            let west_col =
                TileCoord::new(zoom, sw.x, 0).map_err(|e| OsmError::InvalidInput(e.to_string()))?;
            let east_col =
                TileCoord::new(zoom, ne.x, 0).map_err(|e| OsmError::InvalidInput(e.to_string()))?;
            west = west_col.bounds().west;
            east = east_col.bounds().east;
        }
    }

    Ok(expand_bounds_m(
        Bounds::new(south, west, north, east),
        pad_m,
    ))
}

fn expand_bounds_m(bounds: Bounds, meters: f64) -> Bounds {
    if meters <= 0.0 {
        return bounds;
    }

    let lat_pad_deg = meters / 111_320.0;
    let mid_lat = (bounds.south + bounds.north) * 0.5;
    let lng_pad_deg = lat_pad_deg / mid_lat.to_radians().cos().abs().max(1e-6);

    let south = (bounds.south - lat_pad_deg).clamp(-90.0, 90.0);
    let north = (bounds.north + lat_pad_deg).clamp(-90.0, 90.0);
    let lon_span = lon_span_deg(bounds.west, bounds.east);
    if lon_span >= 360.0 - FULL_WORLD_LON_EPS
        || lon_span + 2.0 * lng_pad_deg >= 360.0 - FULL_WORLD_LON_EPS
    {
        return Bounds::new(south.min(north), -180.0, north.max(south), 180.0);
    }

    let west = wrap_lon(bounds.west - lng_pad_deg);
    let east = wrap_lon(bounds.east + lng_pad_deg);

    Bounds::new(south.min(north), west, north.max(south), east)
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

fn lon_span_deg(west: f64, east: f64) -> f64 {
    if west <= east {
        east - west
    } else {
        (180.0 - west) + (east + 180.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    #[test]
    fn expand_bounds_zero_is_noop() {
        let b = Bounds::new(34.0, -118.3, 34.1, -118.2);
        let out = expand_bounds_m(b, 0.0);
        assert!((out.south - b.south).abs() < 1e-12);
        assert!((out.west - b.west).abs() < 1e-12);
        assert!((out.north - b.north).abs() < 1e-12);
        assert!((out.east - b.east).abs() < 1e-12);
    }

    #[test]
    fn expand_bounds_grows_in_all_directions() {
        let b = Bounds::new(34.0, -118.3, 34.1, -118.2);
        let out = expand_bounds_m(b, 500.0);
        assert!(out.south < b.south);
        assert!(out.west < b.west);
        assert!(out.north > b.north);
        assert!(out.east > b.east);
    }

    #[test]
    fn tile_coverage_bounds_keeps_antimeridian_wrap() {
        let bounds = Bounds::new(-1.0, 170.0, 1.0, -170.0);
        let out = tile_coverage_bounds(&bounds, 2, 0.0).expect("tile coverage bounds");
        assert!(out.west > out.east);
        assert!((out.west - 90.0).abs() < 1e-6);
        assert!((out.east + 90.0).abs() < 1e-6);
    }

    #[test]
    fn tile_coverage_bounds_zoom1_crosses_full_world() {
        let bounds = Bounds::new(-1.0, 170.0, 1.0, -170.0);
        let out = tile_coverage_bounds(&bounds, 1, 0.0).expect("tile coverage bounds");
        assert!((out.west + 180.0).abs() < 1e-6);
        assert!((out.east - 180.0).abs() < 1e-6);
    }

    #[test]
    fn expand_bounds_preserves_full_world_longitude() {
        let b = Bounds::new(-10.0, -180.0, 10.0, 180.0);
        let out = expand_bounds_m(b, 500.0);
        assert!((out.west + 180.0).abs() < 1e-6);
        assert!((out.east - 180.0).abs() < 1e-6);
    }

    #[test]
    fn from_pbf_for_traffic_bounds_antimeridian_excludes_far_edges() {
        let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/dateline_synthetic.osm.pbf");
        let bounds = Bounds::new(-1.0, 170.0, 1.0, -170.0);

        let network =
            OsmRoadNetwork::from_pbf_for_traffic_bounds(&fixture, &bounds, 2).expect("network");

        let mut ways: Vec<i64> = network.edges().iter().map(|e| e.way_id).collect();
        ways.sort_unstable();
        ways.dedup();
        assert_eq!(ways, vec![10, 11]);
    }

    #[test]
    fn from_pbf_for_traffic_bounds_antimeridian_zoom1_includes_all_columns() {
        let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/dateline_synthetic.osm.pbf");
        let bounds = Bounds::new(-1.0, 170.0, 1.0, -170.0);

        let network =
            OsmRoadNetwork::from_pbf_for_traffic_bounds(&fixture, &bounds, 1).expect("network");

        let mut ways: Vec<i64> = network.edges().iter().map(|e| e.way_id).collect();
        ways.sort_unstable();
        ways.dedup();
        assert_eq!(ways, vec![10, 11, 12]);
    }

    #[test]
    fn from_pbf_for_traffic_bounds_antimeridian_is_stable_across_zoom_levels() {
        let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/dateline_synthetic.osm.pbf");
        let bounds = Bounds::new(-1.0, 170.0, 1.0, -170.0);

        for zoom in 0..=6 {
            let network = OsmRoadNetwork::from_pbf_for_traffic_bounds(&fixture, &bounds, zoom)
                .expect("network");
            let mut ways: Vec<i64> = network.edges().iter().map(|e| e.way_id).collect();
            ways.sort_unstable();
            ways.dedup();

            let expected = if zoom <= 1 {
                vec![10, 11, 12]
            } else {
                vec![10, 11]
            };
            assert_eq!(ways, expected, "zoom={zoom}");
        }
    }
}
