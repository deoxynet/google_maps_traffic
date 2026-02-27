use crate::geo_utils::{
    bearing_diff_deg, compute_bearing, haversine_distance_m, point_to_segment_distance_m,
    shortest_lng_delta_deg, wrap_lon,
};
use crate::osm::index::IndexedEdge;
use crate::osm::types::{
    EdgeTravelDirection, MatchConfig, MatchOutput, MatchResult, Oneway, OsmEdge, UnmatchedSegment,
};
use crate::types::{CongestionLevel, LatLng, TrafficTile};
use rstar::{AABB, RTree};
use std::collections::HashMap;

const MAX_POLYLINE_SAMPLES: usize = 8;
const MAX_FALLBACK_SCORE: f64 = 2.0;

#[derive(Debug, Clone)]
struct CandidateMatch {
    edge_index: usize,
    direction: EdgeTravelDirection,
    congestion: CongestionLevel,
    distance_m: f64,
    bearing_diff_deg: f64,
    score: f64,
}

#[derive(Clone, Copy)]
struct MatchContext<'a> {
    edges: &'a [OsmEdge],
    tree: &'a RTree<IndexedEdge>,
    config: &'a MatchConfig,
}

#[derive(Clone, Copy)]
struct SegmentInput<'a> {
    midpoint: LatLng,
    polyline: &'a [LatLng],
    segment_bearing: f64,
    congestion: CongestionLevel,
    zoom: u8,
    segment_length_m: f64,
}

pub(super) fn match_tile(
    edges: &[OsmEdge],
    tree: &RTree<IndexedEdge>,
    tile: &TrafficTile,
    config: &MatchConfig,
) -> MatchOutput {
    let (candidates, unmatched) = collect_tile_candidates(edges, tree, tile, config);

    MatchOutput {
        matches: dedupe_candidates(edges, candidates),
        unmatched,
    }
}

pub(super) fn match_tiles(
    edges: &[OsmEdge],
    tree: &RTree<IndexedEdge>,
    tiles: &[TrafficTile],
    config: &MatchConfig,
) -> MatchOutput {
    let mut all_candidates = Vec::new();
    let mut all_unmatched = Vec::new();

    for tile in tiles {
        let (candidates, unmatched) = collect_tile_candidates(edges, tree, tile, config);
        all_candidates.extend(candidates);
        all_unmatched.extend(unmatched);
    }

    MatchOutput {
        matches: dedupe_candidates(edges, all_candidates),
        unmatched: all_unmatched,
    }
}

fn best_candidate(ctx: MatchContext<'_>, segment: SegmentInput<'_>) -> Option<CandidateMatch> {
    if !ctx.config.max_distance_m.is_finite()
        || !ctx.config.max_bearing_diff_deg.is_finite()
        || !ctx.config.distance_weight.is_finite()
        || !ctx.config.bearing_weight.is_finite()
        || ctx.config.max_distance_m <= 0.0
        || ctx.config.max_bearing_diff_deg <= 0.0
    {
        return None;
    }

    let sample_points = sample_polyline_points(segment.polyline, MAX_POLYLINE_SAMPLES);
    if sample_points.is_empty() {
        return None;
    }

    let strict = Thresholds {
        max_distance_m: ctx.config.max_distance_m,
        max_bearing_diff_deg: ctx.config.max_bearing_diff_deg,
    };

    if let Some(best) = best_candidate_midpoint(
        ctx,
        segment.midpoint,
        segment.segment_bearing,
        segment.congestion,
        strict,
    ) {
        return Some(best);
    }

    let adaptive = adaptive_fallback_thresholds(ctx.config, segment.zoom, segment.segment_length_m);
    let fallback = Thresholds {
        max_distance_m: adaptive.max_distance_m.max(strict.max_distance_m),
        max_bearing_diff_deg: adaptive
            .max_bearing_diff_deg
            .max(strict.max_bearing_diff_deg),
    };
    let expanded_thresholds = fallback.max_distance_m > strict.max_distance_m
        || fallback.max_bearing_diff_deg > strict.max_bearing_diff_deg;

    best_candidate_with_thresholds(
        ctx,
        CandidateQuery {
            sample_points: &sample_points,
            segment_bearing: segment.segment_bearing,
            congestion: segment.congestion,
            thresholds: fallback,
            is_fallback: expanded_thresholds,
        },
    )
}

fn best_candidate_midpoint(
    ctx: MatchContext<'_>,
    midpoint: LatLng,
    segment_bearing: f64,
    congestion: CongestionLevel,
    thresholds: Thresholds,
) -> Option<CandidateMatch> {
    let (lat_radius_deg, lng_radius_deg) =
        query_radius_degrees(midpoint.lat, thresholds.max_distance_m);
    let envelopes = wrapped_query_envelopes(
        midpoint.lng - lng_radius_deg,
        midpoint.lng + lng_radius_deg,
        midpoint.lat - lat_radius_deg,
        midpoint.lat + lat_radius_deg,
    );
    if envelopes.is_empty() {
        return None;
    }

    let mut best: Option<CandidateMatch> = None;
    for envelope in &envelopes {
        for indexed in ctx.tree.locate_in_envelope_intersecting(envelope) {
            let edge = &ctx.edges[indexed.edge_index];
            let distance_m = point_to_segment_distance_m(midpoint, edge.start, edge.end);
            if !distance_m.is_finite() || distance_m > thresholds.max_distance_m {
                continue;
            }

            let (bearing_diff, direction) = directional_bearing_match(segment_bearing, edge);
            if !bearing_diff.is_finite() || bearing_diff > thresholds.max_bearing_diff_deg {
                continue;
            }

            let score = ctx.config.distance_weight
                * (distance_m / ctx.config.max_distance_m.max(1e-6))
                + ctx.config.bearing_weight
                    * (bearing_diff / ctx.config.max_bearing_diff_deg.max(1e-6));
            if !score.is_finite() {
                continue;
            }

            let candidate = CandidateMatch {
                edge_index: indexed.edge_index,
                direction,
                congestion,
                distance_m,
                bearing_diff_deg: bearing_diff,
                score,
            };

            if let Some(current) = &best {
                if candidate.score < current.score {
                    best = Some(candidate);
                }
            } else {
                best = Some(candidate);
            }
        }
    }

    best
}

#[derive(Clone, Copy)]
struct CandidateQuery<'a> {
    sample_points: &'a [LatLng],
    segment_bearing: f64,
    congestion: CongestionLevel,
    thresholds: Thresholds,
    is_fallback: bool,
}

fn best_candidate_with_thresholds(
    ctx: MatchContext<'_>,
    query: CandidateQuery<'_>,
) -> Option<CandidateMatch> {
    let envelopes = polyline_query_envelopes(query.sample_points, query.thresholds.max_distance_m);
    if envelopes.is_empty() {
        return None;
    }

    let mut best: Option<CandidateMatch> = None;
    for envelope in &envelopes {
        for indexed in ctx.tree.locate_in_envelope_intersecting(envelope) {
            let edge = &ctx.edges[indexed.edge_index];
            let distance_m = min_distance_to_edge(query.sample_points, edge);
            if !distance_m.is_finite() || distance_m > query.thresholds.max_distance_m {
                continue;
            }

            let (bearing_diff, direction) = directional_bearing_match(query.segment_bearing, edge);
            if !bearing_diff.is_finite() || bearing_diff > query.thresholds.max_bearing_diff_deg {
                continue;
            }

            let score = ctx.config.distance_weight
                * (distance_m / ctx.config.max_distance_m.max(1e-6))
                + ctx.config.bearing_weight
                    * (bearing_diff / ctx.config.max_bearing_diff_deg.max(1e-6));
            if !score.is_finite() {
                continue;
            }
            if query.is_fallback && score > MAX_FALLBACK_SCORE {
                continue;
            }

            let candidate = CandidateMatch {
                edge_index: indexed.edge_index,
                direction,
                congestion: query.congestion,
                distance_m,
                bearing_diff_deg: bearing_diff,
                score,
            };

            if let Some(current) = &best {
                if candidate.score < current.score {
                    best = Some(candidate);
                }
            } else {
                best = Some(candidate);
            }
        }
    }

    best
}

fn collect_tile_candidates(
    edges: &[OsmEdge],
    tree: &RTree<IndexedEdge>,
    tile: &TrafficTile,
    config: &MatchConfig,
) -> (Vec<CandidateMatch>, Vec<UnmatchedSegment>) {
    let ctx = MatchContext {
        edges,
        tree,
        config,
    };
    let mut candidates = Vec::new();
    let mut unmatched = Vec::new();

    for feature in &tile.features {
        for seg in &feature.segments {
            if seg.start_index >= feature.geometry.len()
                || seg.end_index >= feature.geometry.len()
                || seg.start_index >= seg.end_index
            {
                continue;
            }

            let sub_polyline = &feature.geometry[seg.start_index..=seg.end_index];
            let Some(midpoint) = polyline_midpoint(sub_polyline) else {
                continue;
            };
            let bearing = compute_bearing(sub_polyline);
            let segment_len_m = polyline_length_m(sub_polyline);

            if let Some(best) = best_candidate(
                ctx,
                SegmentInput {
                    midpoint,
                    polyline: sub_polyline,
                    segment_bearing: bearing,
                    congestion: seg.congestion,
                    zoom: tile.coord.zoom,
                    segment_length_m: segment_len_m,
                },
            ) {
                candidates.push(best);
            } else {
                unmatched.push(UnmatchedSegment {
                    midpoint,
                    bearing,
                    congestion: seg.congestion,
                });
            }
        }
    }

    (candidates, unmatched)
}

fn dedupe_candidates(edges: &[OsmEdge], candidates: Vec<CandidateMatch>) -> Vec<MatchResult> {
    let mut by_edge: HashMap<(usize, EdgeTravelDirection), MatchResult> = HashMap::new();

    for candidate in candidates {
        let edge = edges[candidate.edge_index].clone();
        let next = MatchResult {
            edge,
            direction: candidate.direction,
            congestion: candidate.congestion,
            distance_m: candidate.distance_m,
            bearing_diff_deg: candidate.bearing_diff_deg,
            score: candidate.score,
        };

        let key = (candidate.edge_index, candidate.direction);
        if let Some(existing) = by_edge.get_mut(&key) {
            if should_replace(existing, &next) {
                *existing = next;
            }
        } else {
            by_edge.insert(key, next);
        }
    }

    let mut matches: Vec<_> = by_edge.into_values().collect();
    matches.sort_by(|a, b| {
        a.score
            .partial_cmp(&b.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    matches
}

fn should_replace(existing: &MatchResult, next: &MatchResult) -> bool {
    let existing_severity = congestion_severity(existing.congestion);
    let next_severity = congestion_severity(next.congestion);
    next_severity > existing_severity
        || (next_severity == existing_severity && next.score < existing.score)
}

fn congestion_severity(congestion: CongestionLevel) -> u8 {
    match congestion {
        CongestionLevel::Unknown(_) => 0,
        CongestionLevel::Normal => 1,
        CongestionLevel::Slow => 2,
        CongestionLevel::Heavy => 3,
        CongestionLevel::Severe => 4,
    }
}

fn directional_bearing_match(segment_bearing: f64, edge: &OsmEdge) -> (f64, EdgeTravelDirection) {
    let reverse = (edge.bearing + 180.0) % 360.0;
    match edge.oneway {
        Oneway::No => {
            let forward = bearing_diff_deg(segment_bearing, edge.bearing);
            let backward = bearing_diff_deg(segment_bearing, reverse);
            if forward <= backward {
                (forward, EdgeTravelDirection::Forward)
            } else {
                (backward, EdgeTravelDirection::Reverse)
            }
        }
        Oneway::Forward => (
            bearing_diff_deg(segment_bearing, edge.bearing),
            EdgeTravelDirection::Forward,
        ),
        Oneway::Reverse => (
            bearing_diff_deg(segment_bearing, reverse),
            EdgeTravelDirection::Reverse,
        ),
    }
}

fn polyline_midpoint(polyline: &[LatLng]) -> Option<LatLng> {
    if polyline.is_empty() {
        return None;
    }
    if polyline.len() == 1 {
        return Some(polyline[0]);
    }

    let total_len_m: f64 = polyline
        .windows(2)
        .map(|w| haversine_distance_m(w[0], w[1]))
        .sum();

    if total_len_m <= f64::EPSILON {
        return Some(polyline[0]);
    }

    let target = total_len_m * 0.5;
    let mut traversed = 0.0;

    for pair in polyline.windows(2) {
        let seg_len = haversine_distance_m(pair[0], pair[1]);
        if traversed + seg_len >= target {
            let t = if seg_len <= f64::EPSILON {
                0.0
            } else {
                (target - traversed) / seg_len
            };
            return Some(LatLng::new(
                pair[0].lat + t * (pair[1].lat - pair[0].lat),
                wrap_lon(pair[0].lng + t * shortest_lng_delta_deg(pair[1].lng - pair[0].lng)),
            ));
        }
        traversed += seg_len;
    }

    Some(polyline[polyline.len() - 1])
}

fn polyline_length_m(polyline: &[LatLng]) -> f64 {
    polyline
        .windows(2)
        .map(|w| haversine_distance_m(w[0], w[1]))
        .sum()
}

fn query_radius_degrees(lat: f64, max_distance_m: f64) -> (f64, f64) {
    let lat_radius_deg = max_distance_m / 111_320.0;
    let cos_lat = lat.to_radians().cos().abs().max(1e-6);
    let lng_radius_deg = lat_radius_deg / cos_lat;
    (lat_radius_deg, lng_radius_deg)
}

#[derive(Debug, Clone, Copy)]
struct Thresholds {
    max_distance_m: f64,
    max_bearing_diff_deg: f64,
}

fn adaptive_fallback_thresholds(
    config: &MatchConfig,
    zoom: u8,
    segment_length_m: f64,
) -> Thresholds {
    let zoom_distance_bonus = match zoom {
        0..=9 => 120.0,
        10 => 90.0,
        11 => 70.0,
        12 => 50.0,
        13 => 30.0,
        14 => 20.0,
        _ => 10.0,
    };
    let max_distance_m = (config.max_distance_m + zoom_distance_bonus).min(200.0);

    let short_segment_bonus = if segment_length_m < 20.0 {
        90.0
    } else if segment_length_m < 50.0 {
        45.0
    } else {
        20.0
    };
    let max_bearing_diff_deg = (config.max_bearing_diff_deg + short_segment_bonus).min(120.0);

    Thresholds {
        max_distance_m,
        max_bearing_diff_deg,
    }
}

fn sample_polyline_points(polyline: &[LatLng], max_samples: usize) -> Vec<LatLng> {
    if polyline.is_empty() || max_samples == 0 {
        return Vec::new();
    }
    if polyline.len() <= max_samples {
        return polyline.to_vec();
    }

    let mut points = Vec::with_capacity(max_samples);
    let denom = (max_samples - 1) as f64;
    let max_idx = (polyline.len() - 1) as f64;

    for i in 0..max_samples {
        let idx = ((i as f64 * max_idx) / denom).round() as usize;
        points.push(polyline[idx]);
    }

    points
}

fn polyline_query_envelopes(
    polyline_points: &[LatLng],
    max_distance_m: f64,
) -> Vec<AABB<[f64; 2]>> {
    let mut min_lat = f64::INFINITY;
    let mut max_lat = f64::NEG_INFINITY;
    let mut min_lng = f64::INFINITY;
    let mut max_lng = f64::NEG_INFINITY;
    let mut lat_sum = 0.0;

    for p in polyline_points {
        min_lat = min_lat.min(p.lat);
        max_lat = max_lat.max(p.lat);
        min_lng = min_lng.min(p.lng);
        max_lng = max_lng.max(p.lng);
        lat_sum += p.lat;
    }

    let mean_lat = lat_sum / polyline_points.len() as f64;
    let (lat_pad, lng_pad) = query_radius_degrees(mean_lat, max_distance_m);
    wrapped_query_envelopes(
        min_lng - lng_pad,
        max_lng + lng_pad,
        min_lat - lat_pad,
        max_lat + lat_pad,
    )
}

fn wrapped_query_envelopes(
    min_lng: f64,
    max_lng: f64,
    min_lat: f64,
    max_lat: f64,
) -> Vec<AABB<[f64; 2]>> {
    if !min_lng.is_finite() || !max_lng.is_finite() || !min_lat.is_finite() || !max_lat.is_finite()
    {
        return Vec::new();
    }

    let min_lat = min_lat.max(-90.0);
    let max_lat = max_lat.min(90.0);
    if min_lat > max_lat {
        return Vec::new();
    }

    if max_lng - min_lng >= 360.0 || (min_lng <= -180.0 && max_lng >= 180.0) {
        return vec![AABB::from_corners([-180.0, min_lat], [180.0, max_lat])];
    }

    if min_lng < -180.0 {
        return vec![
            AABB::from_corners([min_lng + 360.0, min_lat], [180.0, max_lat]),
            AABB::from_corners([-180.0, min_lat], [max_lng, max_lat]),
        ];
    }

    if max_lng > 180.0 {
        return vec![
            AABB::from_corners([min_lng, min_lat], [180.0, max_lat]),
            AABB::from_corners([-180.0, min_lat], [max_lng - 360.0, max_lat]),
        ];
    }

    vec![AABB::from_corners([min_lng, min_lat], [max_lng, max_lat])]
}

fn min_distance_to_edge(polyline_points: &[LatLng], edge: &OsmEdge) -> f64 {
    polyline_points
        .iter()
        .map(|p| point_to_segment_distance_m(*p, edge.start, edge.end))
        .fold(f64::INFINITY, f64::min)
}

#[cfg(test)]
mod tests {
    use super::{
        CandidateMatch, EdgeTravelDirection, MatchConfig, MatchContext, Oneway, OsmEdge,
        SegmentInput, Thresholds, adaptive_fallback_thresholds, best_candidate,
        best_candidate_midpoint, dedupe_candidates, polyline_length_m, polyline_midpoint,
        query_radius_degrees, sample_polyline_points,
    };
    use crate::geo_utils::compute_bearing;
    use crate::osm::index::build_rtree;
    use crate::osm::types::MaxSpeedSource;
    use crate::types::CongestionLevel;
    use crate::types::LatLng;

    #[test]
    fn longitude_radius_expands_at_high_latitudes() {
        let (lat_eq, lng_eq) = query_radius_degrees(0.0, 50.0);
        let (lat_hi, lng_hi) = query_radius_degrees(60.0, 50.0);

        assert!((lat_eq - lat_hi).abs() < 1e-12);
        assert!(lng_hi > lng_eq);
    }

    #[test]
    fn adaptive_thresholds_expand_for_low_zoom_and_short_segments() {
        let cfg = MatchConfig::default();
        let low_zoom_short = adaptive_fallback_thresholds(&cfg, 10, 10.0);
        let high_zoom_long = adaptive_fallback_thresholds(&cfg, 15, 200.0);

        assert!(low_zoom_short.max_distance_m > high_zoom_long.max_distance_m);
        assert!(low_zoom_short.max_bearing_diff_deg > high_zoom_long.max_bearing_diff_deg);
    }

    #[test]
    fn polyline_sampling_respects_max_samples() {
        let polyline: Vec<_> = (0..100)
            .map(|i| LatLng::new(34.0 + i as f64 * 1e-4, -118.0))
            .collect();

        let sampled = sample_polyline_points(&polyline, 16);
        assert_eq!(sampled.len(), 16);
        assert_eq!(sampled.first().copied(), polyline.first().copied());
        assert_eq!(sampled.last().copied(), polyline.last().copied());
    }

    #[test]
    fn polyline_fallback_runs_even_when_adaptive_caps_below_strict() {
        let edges = vec![OsmEdge {
            way_id: 1,
            start_node_id: 10,
            end_node_id: 11,
            start: LatLng::new(0.0, 0.0),
            end: LatLng::new(0.0, 0.01),
            bearing: 90.0,
            highway: "primary".to_string(),
            oneway: Oneway::No,
            max_speed_kph: 50.0,
            max_speed_source: MaxSpeedSource::TagKph,
            max_speed_forward_kph: 50.0,
            max_speed_forward_source: MaxSpeedSource::TagKph,
            max_speed_backward_kph: 50.0,
            max_speed_backward_source: MaxSpeedSource::TagKph,
        }];
        let tree = build_rtree(&edges);

        let cfg = MatchConfig {
            max_distance_m: 300.0,
            max_bearing_diff_deg: 120.0,
            ..MatchConfig::default()
        };
        let ctx = MatchContext {
            edges: &edges,
            tree: &tree,
            config: &cfg,
        };

        let polyline = [
            LatLng::new(0.0, 0.001),
            LatLng::new(0.004, 0.005),
            LatLng::new(0.0, 0.009),
        ];
        let midpoint = polyline_midpoint(&polyline).expect("midpoint");
        let segment_bearing = compute_bearing(&polyline);

        assert!(
            best_candidate_midpoint(
                ctx,
                midpoint,
                segment_bearing,
                CongestionLevel::Normal,
                Thresholds {
                    max_distance_m: cfg.max_distance_m,
                    max_bearing_diff_deg: cfg.max_bearing_diff_deg
                }
            )
            .is_none()
        );

        let candidate = best_candidate(
            ctx,
            SegmentInput {
                midpoint,
                polyline: &polyline,
                segment_bearing,
                congestion: CongestionLevel::Normal,
                zoom: 10,
                segment_length_m: polyline_length_m(&polyline),
            },
        )
        .expect("polyline fallback should match");

        assert_eq!(candidate.edge_index, 0);
        assert!(candidate.distance_m < 5.0);
    }

    #[test]
    fn polyline_midpoint_handles_antimeridian_crossing() {
        let polyline = [LatLng::new(0.0, 179.9), LatLng::new(0.0, -179.9)];
        let midpoint = polyline_midpoint(&polyline).expect("midpoint");
        assert!(midpoint.lat.abs() < 1e-6);
        assert!(
            (midpoint.lng.abs() - 180.0).abs() < 0.2,
            "expected midpoint near dateline, got {}",
            midpoint.lng
        );
    }

    #[test]
    fn midpoint_query_wraps_across_antimeridian() {
        let edges = vec![OsmEdge {
            way_id: 1,
            start_node_id: 10,
            end_node_id: 11,
            start: LatLng::new(0.0, -179.95),
            end: LatLng::new(0.01, -179.95),
            bearing: 0.0,
            highway: "primary".to_string(),
            oneway: Oneway::No,
            max_speed_kph: 50.0,
            max_speed_source: MaxSpeedSource::TagKph,
            max_speed_forward_kph: 50.0,
            max_speed_forward_source: MaxSpeedSource::TagKph,
            max_speed_backward_kph: 50.0,
            max_speed_backward_source: MaxSpeedSource::TagKph,
        }];
        let tree = build_rtree(&edges);
        let cfg = MatchConfig {
            max_distance_m: 12_000.0,
            max_bearing_diff_deg: 180.0,
            ..MatchConfig::default()
        };
        let ctx = MatchContext {
            edges: &edges,
            tree: &tree,
            config: &cfg,
        };

        let best = best_candidate_midpoint(
            ctx,
            LatLng::new(0.0, 179.999),
            0.0,
            CongestionLevel::Normal,
            Thresholds {
                max_distance_m: cfg.max_distance_m,
                max_bearing_diff_deg: cfg.max_bearing_diff_deg,
            },
        )
        .expect("wrapped query should find nearby edge across dateline");

        assert_eq!(best.edge_index, 0);
    }

    #[test]
    fn non_finite_config_weights_disable_matching() {
        let edges = vec![OsmEdge {
            way_id: 1,
            start_node_id: 10,
            end_node_id: 11,
            start: LatLng::new(0.0, 0.0),
            end: LatLng::new(0.0, 0.01),
            bearing: 90.0,
            highway: "primary".to_string(),
            oneway: Oneway::No,
            max_speed_kph: 50.0,
            max_speed_source: MaxSpeedSource::TagKph,
            max_speed_forward_kph: 50.0,
            max_speed_forward_source: MaxSpeedSource::TagKph,
            max_speed_backward_kph: 50.0,
            max_speed_backward_source: MaxSpeedSource::TagKph,
        }];
        let tree = build_rtree(&edges);
        let cfg = MatchConfig {
            distance_weight: f64::NAN,
            ..MatchConfig::default()
        };
        let ctx = MatchContext {
            edges: &edges,
            tree: &tree,
            config: &cfg,
        };
        let polyline = [LatLng::new(0.0, 0.001), LatLng::new(0.0, 0.009)];
        let midpoint = polyline_midpoint(&polyline).expect("midpoint");
        let segment_bearing = compute_bearing(&polyline);

        let candidate = best_candidate(
            ctx,
            SegmentInput {
                midpoint,
                polyline: &polyline,
                segment_bearing,
                congestion: CongestionLevel::Normal,
                zoom: 14,
                segment_length_m: polyline_length_m(&polyline),
            },
        );

        assert!(candidate.is_none());
    }

    #[test]
    fn dedupe_candidates_keeps_opposite_directions_separate() {
        let edges = vec![OsmEdge {
            way_id: 1,
            start_node_id: 10,
            end_node_id: 11,
            start: LatLng::new(0.0, 0.0),
            end: LatLng::new(0.0, 0.01),
            bearing: 90.0,
            highway: "primary".to_string(),
            oneway: Oneway::No,
            max_speed_kph: 50.0,
            max_speed_source: MaxSpeedSource::TagKph,
            max_speed_forward_kph: 50.0,
            max_speed_forward_source: MaxSpeedSource::TagKph,
            max_speed_backward_kph: 50.0,
            max_speed_backward_source: MaxSpeedSource::TagKph,
        }];

        let candidates = vec![
            CandidateMatch {
                edge_index: 0,
                direction: EdgeTravelDirection::Forward,
                congestion: CongestionLevel::Slow,
                distance_m: 2.0,
                bearing_diff_deg: 4.0,
                score: 0.2,
            },
            CandidateMatch {
                edge_index: 0,
                direction: EdgeTravelDirection::Reverse,
                congestion: CongestionLevel::Heavy,
                distance_m: 1.0,
                bearing_diff_deg: 3.0,
                score: 0.1,
            },
        ];

        let out = dedupe_candidates(&edges, candidates);
        assert_eq!(out.len(), 2);
        assert!(out.iter().any(|m| {
            m.direction == EdgeTravelDirection::Forward
                && matches!(m.congestion, CongestionLevel::Slow)
        }));
        assert!(out.iter().any(|m| {
            m.direction == EdgeTravelDirection::Reverse
                && matches!(m.congestion, CongestionLevel::Heavy)
        }));
    }
}
