use crate::types::{Bounds, CongestionLevel, DayOfWeek, LatLng};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Oneway {
    No,
    Forward,
    Reverse,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum EdgeTravelDirection {
    Forward,
    Reverse,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MaxSpeedSource {
    TagKph,
    TagMph,
    ZoneTag,
    HighwayDefault,
}

#[derive(Debug, Clone)]
pub struct OsmEdge {
    pub way_id: i64,
    pub start_node_id: i64,
    pub end_node_id: i64,
    pub start: LatLng,
    pub end: LatLng,
    pub bearing: f64,
    pub highway: String,
    pub oneway: Oneway,
    pub max_speed_kph: f64,
    pub max_speed_source: MaxSpeedSource,
    pub max_speed_forward_kph: f64,
    pub max_speed_forward_source: MaxSpeedSource,
    pub max_speed_backward_kph: f64,
    pub max_speed_backward_source: MaxSpeedSource,
}

#[derive(Debug, Clone)]
pub struct MatchConfig {
    pub max_distance_m: f64,
    pub max_bearing_diff_deg: f64,
    pub distance_weight: f64,
    pub bearing_weight: f64,
}

#[derive(Debug, Clone, Copy)]
pub struct TypicalCalibrationSpec<'a> {
    pub bounds: &'a Bounds,
    pub zoom: u8,
    pub day: DayOfWeek,
    pub hours: &'a [u8],
    pub match_config: &'a MatchConfig,
    pub base: &'a SpeedEstimationConfig,
}

impl Default for MatchConfig {
    fn default() -> Self {
        Self {
            max_distance_m: 50.0,
            max_bearing_diff_deg: 30.0,
            distance_weight: 0.6,
            bearing_weight: 0.4,
        }
    }
}

impl MatchConfig {
    /// Recommended starting point for a given map zoom level.
    ///
    /// Lower zoom levels use more generalized geometry and benefit from a wider
    /// search radius and looser bearing gate.
    pub fn for_zoom(zoom: u8) -> Self {
        let (max_distance_m, max_bearing_diff_deg) = match zoom {
            0..=10 => (100.0, 55.0),
            11 => (90.0, 50.0),
            12 => (80.0, 45.0),
            13 => (65.0, 40.0),
            14 => (50.0, 35.0),
            _ => (40.0, 30.0),
        };

        Self {
            max_distance_m,
            max_bearing_diff_deg,
            ..Self::default()
        }
    }
}

#[derive(Debug, Clone)]
pub struct SpeedEstimationConfig {
    pub normal_factor: f64,
    pub slow_factor: f64,
    pub heavy_factor: f64,
    pub severe_factor: f64,
    pub unknown_factor: f64,
    pub hourly_factors: [f64; 24],
    pub min_speed_kph: f64,
}

impl Default for SpeedEstimationConfig {
    fn default() -> Self {
        let mut hourly_factors = [1.0; 24];
        hourly_factors[6] = 0.95;
        for factor in hourly_factors.iter_mut().take(10).skip(7) {
            *factor = 0.85;
        }
        for factor in hourly_factors.iter_mut().take(16).skip(10) {
            *factor = 0.92;
        }
        for factor in hourly_factors.iter_mut().take(19).skip(16) {
            *factor = 0.82;
        }
        for factor in hourly_factors.iter_mut().take(22).skip(19) {
            *factor = 0.90;
        }
        hourly_factors[22] = 0.97;
        hourly_factors[23] = 0.97;

        Self {
            normal_factor: 0.95,
            slow_factor: 0.70,
            heavy_factor: 0.45,
            severe_factor: 0.20,
            unknown_factor: 0.65,
            hourly_factors,
            min_speed_kph: 5.0,
        }
    }
}

impl SpeedEstimationConfig {
    pub fn congestion_factor(&self, congestion: CongestionLevel) -> f64 {
        match congestion {
            CongestionLevel::Normal => self.normal_factor,
            CongestionLevel::Slow => self.slow_factor,
            CongestionLevel::Heavy => self.heavy_factor,
            CongestionLevel::Severe => self.severe_factor,
            CongestionLevel::Unknown(_) => self.unknown_factor,
        }
    }

    pub fn hour_factor(&self, hour: u8) -> f64 {
        self.hourly_factors[hour.min(23) as usize]
    }

    pub fn set_hour_factor(&mut self, hour: u8, factor: f64) {
        let sanitized = if factor.is_finite() {
            factor.clamp(0.4, 1.2)
        } else {
            1.0
        };
        self.hourly_factors[hour.min(23) as usize] = sanitized;
    }

    pub fn estimate_from_limit_kph(
        &self,
        speed_limit_kph: f64,
        congestion: CongestionLevel,
        hour_of_day: Option<u8>,
    ) -> f64 {
        if !speed_limit_kph.is_finite() || speed_limit_kph <= 0.0 {
            return 0.0;
        }

        let congestion_factor = self.congestion_factor(congestion);
        let hour_factor = hour_of_day.map(|h| self.hour_factor(h)).unwrap_or(1.0);
        let mut estimated = speed_limit_kph * congestion_factor * hour_factor;
        if !estimated.is_finite() {
            estimated = speed_limit_kph;
        }

        let min_speed = if self.min_speed_kph.is_finite() {
            self.min_speed_kph.max(0.0)
        } else {
            0.0
        };
        let lower = min_speed.min(speed_limit_kph);
        estimated.clamp(lower, speed_limit_kph)
    }
}

#[derive(Debug, Clone)]
pub struct MatchResult {
    pub edge: OsmEdge,
    pub direction: EdgeTravelDirection,
    pub congestion: CongestionLevel,
    pub distance_m: f64,
    pub bearing_diff_deg: f64,
    pub score: f64,
}

impl MatchResult {
    /// Estimate practical travel speed using this edge's legal speed limit,
    /// live congestion level, and optional hour-of-day (0..23).
    ///
    /// This is direction-agnostic and uses `edge.max_speed_kph`.
    pub fn estimate_speed_kph(
        &self,
        hour_of_day: Option<u8>,
        config: &SpeedEstimationConfig,
    ) -> f64 {
        config.estimate_from_limit_kph(self.edge.max_speed_kph, self.congestion, hour_of_day)
    }

    /// Estimate speed in the direction this traffic segment was matched to.
    ///
    /// Returns `None` when the stored direction is not legal under oneway
    /// restrictions (should not normally happen for matcher-produced results).
    pub fn estimate_speed_in_match_direction_kph(
        &self,
        hour_of_day: Option<u8>,
        config: &SpeedEstimationConfig,
    ) -> Option<f64> {
        self.estimate_speed_directional_kph(self.direction, hour_of_day, config)
    }

    /// Estimate speed in a specific edge direction.
    ///
    /// Returns `None` if requested direction is not legal for the edge's
    /// `oneway` restrictions.
    pub fn estimate_speed_directional_kph(
        &self,
        direction: EdgeTravelDirection,
        hour_of_day: Option<u8>,
        config: &SpeedEstimationConfig,
    ) -> Option<f64> {
        let speed_limit = self.legal_speed_limit_for_direction_kph(direction)?;
        Some(config.estimate_from_limit_kph(speed_limit, self.congestion, hour_of_day))
    }

    pub fn legal_speed_limit_for_direction_kph(
        &self,
        direction: EdgeTravelDirection,
    ) -> Option<f64> {
        match (self.edge.oneway, direction) {
            (Oneway::Forward, EdgeTravelDirection::Reverse) => None,
            (Oneway::Reverse, EdgeTravelDirection::Forward) => None,
            (_, EdgeTravelDirection::Forward) => Some(self.edge.max_speed_forward_kph),
            (_, EdgeTravelDirection::Reverse) => Some(self.edge.max_speed_backward_kph),
        }
    }
}

impl SpeedEstimationConfig {
    /// Derive a single hour factor from a set of matched typical-traffic results.
    ///
    /// Returns `None` if `matches` is empty.
    pub fn calibrated_hour_factor_from_matches(&self, matches: &[MatchResult]) -> Option<f64> {
        if matches.is_empty() {
            return None;
        }

        let mut weighted_sum = 0.0;
        let mut weight_total = 0.0;
        for m in matches {
            let weight = m.edge.max_speed_kph.max(1.0);
            weighted_sum += self.congestion_factor(m.congestion) * weight;
            weight_total += weight;
        }

        if weight_total <= f64::EPSILON {
            return None;
        }

        let mean_congestion_factor = weighted_sum / weight_total;
        let normalized = mean_congestion_factor / self.normal_factor.max(1e-6);
        Some(normalized.clamp(0.4, 1.2))
    }
}

#[derive(Debug, Clone)]
pub struct UnmatchedSegment {
    pub midpoint: LatLng,
    pub bearing: f64,
    pub congestion: CongestionLevel,
}

#[derive(Debug, Clone, Default)]
pub struct MatchOutput {
    pub matches: Vec<MatchResult>,
    pub unmatched: Vec<UnmatchedSegment>,
}

#[cfg(test)]
mod tests {
    use super::{
        EdgeTravelDirection, MatchConfig, MatchResult, MaxSpeedSource, Oneway, OsmEdge,
        SpeedEstimationConfig,
    };
    use crate::types::{CongestionLevel, LatLng};

    #[test]
    fn zoom_presets_loosen_at_lower_zoom() {
        let z10 = MatchConfig::for_zoom(10);
        let z15 = MatchConfig::for_zoom(15);

        assert!(z10.max_distance_m > z15.max_distance_m);
        assert!(z10.max_bearing_diff_deg > z15.max_bearing_diff_deg);
    }

    #[test]
    fn estimate_speed_respects_congestion_and_hour() {
        let edge = OsmEdge {
            way_id: 1,
            start_node_id: 1,
            end_node_id: 2,
            start: LatLng::new(34.0, -118.0),
            end: LatLng::new(34.0, -117.99),
            bearing: 90.0,
            highway: "primary".to_string(),
            oneway: Oneway::No,
            max_speed_kph: 80.0,
            max_speed_source: MaxSpeedSource::TagKph,
            max_speed_forward_kph: 80.0,
            max_speed_forward_source: MaxSpeedSource::TagKph,
            max_speed_backward_kph: 80.0,
            max_speed_backward_source: MaxSpeedSource::TagKph,
        };
        let result = MatchResult {
            edge,
            direction: EdgeTravelDirection::Forward,
            congestion: CongestionLevel::Heavy,
            distance_m: 5.0,
            bearing_diff_deg: 2.0,
            score: 0.1,
        };

        let cfg = SpeedEstimationConfig::default();
        let off_peak = result.estimate_speed_kph(Some(2), &cfg);
        let rush = result.estimate_speed_kph(Some(17), &cfg);

        assert!(off_peak <= result.edge.max_speed_kph);
        assert!(rush < off_peak);
    }

    #[test]
    fn directional_estimate_respects_oneway() {
        let edge = OsmEdge {
            way_id: 1,
            start_node_id: 1,
            end_node_id: 2,
            start: LatLng::new(34.0, -118.0),
            end: LatLng::new(34.0, -117.99),
            bearing: 90.0,
            highway: "primary".to_string(),
            oneway: Oneway::Forward,
            max_speed_kph: 60.0,
            max_speed_source: MaxSpeedSource::TagKph,
            max_speed_forward_kph: 60.0,
            max_speed_forward_source: MaxSpeedSource::TagKph,
            max_speed_backward_kph: 40.0,
            max_speed_backward_source: MaxSpeedSource::TagKph,
        };
        let result = MatchResult {
            edge,
            direction: EdgeTravelDirection::Forward,
            congestion: CongestionLevel::Normal,
            distance_m: 1.0,
            bearing_diff_deg: 1.0,
            score: 0.01,
        };
        let cfg = SpeedEstimationConfig::default();

        let forward = result
            .estimate_speed_directional_kph(EdgeTravelDirection::Forward, Some(12), &cfg)
            .unwrap();
        assert!(forward > 0.0);
        assert!(
            result
                .estimate_speed_directional_kph(EdgeTravelDirection::Reverse, Some(12), &cfg)
                .is_none()
        );
    }

    #[test]
    fn calibration_from_matches_returns_factor() {
        let edge = OsmEdge {
            way_id: 1,
            start_node_id: 1,
            end_node_id: 2,
            start: LatLng::new(34.0, -118.0),
            end: LatLng::new(34.0, -117.99),
            bearing: 90.0,
            highway: "primary".to_string(),
            oneway: Oneway::No,
            max_speed_kph: 80.0,
            max_speed_source: MaxSpeedSource::HighwayDefault,
            max_speed_forward_kph: 80.0,
            max_speed_forward_source: MaxSpeedSource::HighwayDefault,
            max_speed_backward_kph: 80.0,
            max_speed_backward_source: MaxSpeedSource::HighwayDefault,
        };

        let matches = vec![
            MatchResult {
                edge: edge.clone(),
                direction: EdgeTravelDirection::Forward,
                congestion: CongestionLevel::Slow,
                distance_m: 1.0,
                bearing_diff_deg: 1.0,
                score: 0.1,
            },
            MatchResult {
                edge,
                direction: EdgeTravelDirection::Reverse,
                congestion: CongestionLevel::Heavy,
                distance_m: 1.0,
                bearing_diff_deg: 1.0,
                score: 0.1,
            },
        ];

        let cfg = SpeedEstimationConfig::default();
        let factor = cfg.calibrated_hour_factor_from_matches(&matches).unwrap();
        assert!(factor < 1.0);
        assert!(factor > 0.4);
    }

    #[test]
    fn estimate_speed_handles_low_speed_limit_without_panic() {
        let cfg = SpeedEstimationConfig::default();
        let estimated = cfg.estimate_from_limit_kph(3.0, CongestionLevel::Normal, Some(12));
        assert!((estimated - 3.0).abs() < 1e-9);
    }

    #[test]
    fn set_hour_factor_sanitizes_nan() {
        let mut cfg = SpeedEstimationConfig::default();
        cfg.set_hour_factor(9, f64::NAN);
        assert!((cfg.hour_factor(9) - 1.0).abs() < 1e-12);
    }
}
