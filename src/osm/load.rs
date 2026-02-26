use crate::geo_utils::compute_bearing;
use crate::osm::OsmError;
use crate::osm::types::{MaxSpeedSource, Oneway, OsmEdge};
use crate::types::{Bounds, LatLng};
use osmpbf::{Element, ElementReader};
use std::collections::{HashMap, HashSet};
use std::path::Path;

const KPH_PER_MPH: f64 = 1.609_344;
const FULL_WORLD_LON_EPS: f64 = 1e-9;

#[derive(Debug, Clone, Copy)]
struct ParsedSpeed {
    kph: f64,
    source: MaxSpeedSource,
}

#[derive(Debug, Clone, Copy)]
struct ResolvedEdgeSpeed {
    nominal: ParsedSpeed,
    forward: ParsedSpeed,
    backward: ParsedSpeed,
}

#[derive(Debug)]
struct WaySpec {
    way_id: i64,
    highway: String,
    oneway: Oneway,
    maxspeed: Option<ParsedSpeed>,
    maxspeed_forward: Option<ParsedSpeed>,
    maxspeed_backward: Option<ParsedSpeed>,
    node_refs: Vec<i64>,
}

pub(super) fn load_edges_from_pbf(path: &Path) -> Result<Vec<OsmEdge>, OsmError> {
    let (ways, needed_node_ids) = collect_way_specs(path)?;
    let node_lookup = resolve_way_nodes(path, &needed_node_ids)?;
    Ok(build_edges(ways, &node_lookup, None))
}

pub(super) fn load_edges_from_pbf_bounded(
    path: &Path,
    bounds: &Bounds,
) -> Result<Vec<OsmEdge>, OsmError> {
    let (ways, needed_node_ids) = collect_way_specs(path)?;
    let node_lookup = resolve_way_nodes(path, &needed_node_ids)?;
    Ok(build_edges(ways, &node_lookup, Some(bounds)))
}

fn collect_way_specs(path: &Path) -> Result<(Vec<WaySpec>, HashSet<i64>), OsmError> {
    let mut ways = Vec::new();
    let mut needed_node_ids = HashSet::new();

    let reader = ElementReader::from_path(path).map_err(|e| OsmError::Io(e.into()))?;

    reader
        .for_each(|element| {
            if let Element::Way(way) = element {
                let mut highway: Option<String> = None;
                let mut explicit_oneway: Option<&str> = None;
                let mut junction: Option<String> = None;
                let mut maxspeed = None;
                let mut maxspeed_forward = None;
                let mut maxspeed_backward = None;
                for (key, value) in way.tags() {
                    match key {
                        "highway" => {
                            highway = Some(value.to_ascii_lowercase());
                        }
                        "oneway" => {
                            explicit_oneway = Some(value);
                        }
                        "junction" => {
                            junction = Some(value.to_ascii_lowercase());
                        }
                        "maxspeed" => {
                            maxspeed = parse_maxspeed_tag(value);
                        }
                        "maxspeed:forward" => {
                            maxspeed_forward = parse_maxspeed_tag(value);
                        }
                        "maxspeed:backward" => {
                            maxspeed_backward = parse_maxspeed_tag(value);
                        }
                        _ => {}
                    }
                }

                let Some(highway) = highway else { return };
                if !is_supported_highway(&highway) {
                    return;
                }

                let refs: Vec<i64> = way.refs().collect();
                if refs.len() < 2 {
                    return;
                }

                for node_id in &refs {
                    needed_node_ids.insert(*node_id);
                }

                let oneway = infer_oneway(&highway, junction.as_deref(), explicit_oneway);

                ways.push(WaySpec {
                    way_id: way.id(),
                    highway,
                    oneway,
                    maxspeed,
                    maxspeed_forward,
                    maxspeed_backward,
                    node_refs: refs,
                });
            }
        })
        .map_err(|e| OsmError::Pbf(format!("failed reading ways: {e}")))?;

    Ok((ways, needed_node_ids))
}

fn resolve_way_nodes(
    path: &Path,
    needed_node_ids: &HashSet<i64>,
) -> Result<HashMap<i64, LatLng>, OsmError> {
    let mut node_lookup = HashMap::with_capacity(needed_node_ids.len());

    let reader = ElementReader::from_path(path).map_err(|e| OsmError::Io(e.into()))?;

    reader
        .for_each(|element| match element {
            Element::Node(node) => {
                let node_id = node.id();
                if needed_node_ids.contains(&node_id) {
                    node_lookup.insert(node_id, LatLng::new(node.lat(), node.lon()));
                }
            }
            Element::DenseNode(node) => {
                let node_id = node.id();
                if needed_node_ids.contains(&node_id) {
                    node_lookup.insert(node_id, LatLng::new(node.lat(), node.lon()));
                }
            }
            _ => {}
        })
        .map_err(|e| OsmError::Pbf(format!("failed reading nodes: {e}")))?;

    Ok(node_lookup)
}

fn build_edges(
    ways: Vec<WaySpec>,
    node_lookup: &HashMap<i64, LatLng>,
    bounds: Option<&Bounds>,
) -> Vec<OsmEdge> {
    let mut edges = Vec::new();

    for way in ways {
        for pair in way.node_refs.windows(2) {
            let start_id = pair[0];
            let end_id = pair[1];

            let Some(&start) = node_lookup.get(&start_id) else {
                continue;
            };
            let Some(&end) = node_lookup.get(&end_id) else {
                continue;
            };

            if let Some(bounds) = bounds
                && !edge_intersects_bounds(start, end, bounds, 100.0)
            {
                continue;
            }

            let bearing = compute_bearing(&[start, end]);
            let max_speed = resolve_edge_speed(
                &way.highway,
                way.oneway,
                way.maxspeed,
                way.maxspeed_forward,
                way.maxspeed_backward,
            );
            edges.push(OsmEdge {
                way_id: way.way_id,
                start_node_id: start_id,
                end_node_id: end_id,
                start,
                end,
                bearing,
                highway: way.highway.clone(),
                oneway: way.oneway,
                max_speed_kph: max_speed.nominal.kph,
                max_speed_source: max_speed.nominal.source,
                max_speed_forward_kph: max_speed.forward.kph,
                max_speed_forward_source: max_speed.forward.source,
                max_speed_backward_kph: max_speed.backward.kph,
                max_speed_backward_source: max_speed.backward.source,
            });
        }
    }

    edges
}

fn edge_intersects_bounds(start: LatLng, end: LatLng, bounds: &Bounds, buffer_m: f64) -> bool {
    let lat_buffer_deg = buffer_m / 111_320.0;
    let mean_lat_rad = ((bounds.south + bounds.north) / 2.0).to_radians();
    let lng_buffer_deg = lat_buffer_deg / mean_lat_rad.cos().abs().max(1e-6);

    let min_y = (bounds.south - lat_buffer_deg).clamp(-90.0, 90.0);
    let max_y = (bounds.north + lat_buffer_deg).clamp(-90.0, 90.0);
    let lon_span = lon_span_deg(bounds.west, bounds.east);
    if lon_span >= 360.0 - FULL_WORLD_LON_EPS
        || lon_span + 2.0 * lng_buffer_deg >= 360.0 - FULL_WORLD_LON_EPS
    {
        return segment_intersects_rect(
            start,
            end,
            Rect {
                min_x: -180.0,
                max_x: 180.0,
                min_y,
                max_y,
            },
        );
    }

    let min_x = wrap_lon(bounds.west - lng_buffer_deg);
    let max_x = wrap_lon(bounds.east + lng_buffer_deg);

    if min_x <= max_x {
        return segment_intersects_rect(
            start,
            end,
            Rect {
                min_x,
                max_x,
                min_y,
                max_y,
            },
        );
    }

    segment_intersects_rect(
        start,
        end,
        Rect {
            min_x,
            max_x: 180.0,
            min_y,
            max_y,
        },
    ) || segment_intersects_rect(
        start,
        end,
        Rect {
            min_x: -180.0,
            max_x,
            min_y,
            max_y,
        },
    )
}

/// Liang-Barsky line clipping test for segment-vs-AABB intersection.
fn segment_intersects_rect(start: LatLng, end: LatLng, rect: Rect) -> bool {
    let x0 = start.lng;
    let y0 = start.lat;
    let x1 = end.lng;
    let y1 = end.lat;

    let raw_delta = x1 - x0;
    if raw_delta.abs() <= 180.0 {
        return segment_intersects_rect_planar(x0, y0, x1, y1, rect);
    }

    // Dateline-crossing segment: evaluate the shortest-path unwrapped variant
    // and test against rect copies shifted by +/- 360 degrees.
    let x1_short = if raw_delta > 0.0 {
        x1 - 360.0
    } else {
        x1 + 360.0
    };
    segment_intersects_rect_planar(x0, y0, x1_short, y1, rect)
        || segment_intersects_rect_planar(x0, y0, x1_short, y1, shifted_rect(rect, 360.0))
        || segment_intersects_rect_planar(x0, y0, x1_short, y1, shifted_rect(rect, -360.0))
}

fn segment_intersects_rect_planar(x0: f64, y0: f64, x1: f64, y1: f64, rect: Rect) -> bool {
    if !x0.is_finite() || !y0.is_finite() || !x1.is_finite() || !y1.is_finite() {
        return false;
    }

    let dx = x1 - x0;
    let dy = y1 - y0;

    let p = [-dx, dx, -dy, dy];
    let q = [
        x0 - rect.min_x,
        rect.max_x - x0,
        y0 - rect.min_y,
        rect.max_y - y0,
    ];

    let mut u1: f64 = 0.0;
    let mut u2: f64 = 1.0;

    for i in 0..4 {
        if p[i].abs() <= f64::EPSILON {
            if q[i] < 0.0 {
                return false;
            }
            continue;
        }

        let t = q[i] / p[i];
        if p[i] < 0.0 {
            u1 = u1.max(t);
        } else {
            u2 = u2.min(t);
        }

        if u1 > u2 {
            return false;
        }
    }

    true
}

fn shifted_rect(rect: Rect, dx: f64) -> Rect {
    Rect {
        min_x: rect.min_x + dx,
        max_x: rect.max_x + dx,
        min_y: rect.min_y,
        max_y: rect.max_y,
    }
}

#[derive(Debug, Clone, Copy)]
struct Rect {
    min_x: f64,
    max_x: f64,
    min_y: f64,
    max_y: f64,
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

fn parse_maxspeed_tag(value: &str) -> Option<ParsedSpeed> {
    let raw = value.trim();
    if raw.is_empty() {
        return None;
    }

    let token = raw
        .split(';')
        .next()
        .unwrap_or(raw)
        .trim()
        .to_ascii_lowercase();
    if token.is_empty()
        || matches!(
            token.as_str(),
            "none" | "signals" | "variable" | "walk" | "unknown"
        )
    {
        return None;
    }

    if let Some(kph) = parse_us_zone_speed_kph(&token) {
        return Some(ParsedSpeed {
            kph,
            source: MaxSpeedSource::ZoneTag,
        });
    }

    let number = parse_first_number(&token)?;
    if !number.is_finite() || !(3.0..=200.0).contains(&number) {
        return None;
    }

    if token.contains("mph") {
        return Some(ParsedSpeed {
            kph: number * KPH_PER_MPH,
            source: MaxSpeedSource::TagMph,
        });
    }

    Some(ParsedSpeed {
        kph: number,
        source: MaxSpeedSource::TagKph,
    })
}

fn parse_us_zone_speed_kph(token: &str) -> Option<f64> {
    if !token.starts_with("us:") {
        return None;
    }
    if token == "us:motorway" || token.ends_with(":motorway") {
        return Some(65.0 * KPH_PER_MPH);
    }
    if token == "us:rural" || token.ends_with(":rural") {
        return Some(55.0 * KPH_PER_MPH);
    }
    if token == "us:urban" || token.ends_with(":urban") {
        return Some(30.0 * KPH_PER_MPH);
    }
    if token == "us:residential" || token.ends_with(":residential") {
        return Some(25.0 * KPH_PER_MPH);
    }
    if token == "us:school" || token.ends_with(":school") {
        return Some(20.0 * KPH_PER_MPH);
    }
    None
}

fn parse_first_number(text: &str) -> Option<f64> {
    text.split(|c: char| !(c.is_ascii_digit() || c == '.'))
        .find(|part| !part.is_empty())
        .and_then(|part| part.parse::<f64>().ok())
}

fn resolve_edge_speed(
    highway: &str,
    oneway: Oneway,
    general: Option<ParsedSpeed>,
    forward: Option<ParsedSpeed>,
    backward: Option<ParsedSpeed>,
) -> ResolvedEdgeSpeed {
    let default = default_speed_for_highway(highway);
    let forward_speed = forward.or(general).unwrap_or(default);
    let backward_speed = backward.or(general).unwrap_or(default);

    let nominal = match oneway {
        Oneway::Forward => forward_speed,
        Oneway::Reverse => backward_speed,
        Oneway::No => {
            if let Some(general) = general {
                general
            } else if let (Some(forward), Some(backward)) = (forward, backward) {
                ParsedSpeed {
                    kph: (forward.kph + backward.kph) / 2.0,
                    source: forward.source,
                }
            } else if let Some(forward) = forward {
                forward
            } else if let Some(backward) = backward {
                backward
            } else {
                default
            }
        }
    };

    ResolvedEdgeSpeed {
        nominal,
        forward: match oneway {
            Oneway::Reverse => backward_speed,
            _ => forward_speed,
        },
        backward: match oneway {
            Oneway::Forward => forward_speed,
            _ => backward_speed,
        },
    }
}

fn default_speed_for_highway(highway: &str) -> ParsedSpeed {
    let kph = match highway {
        "motorway" => 105.0,
        "motorway_link" => 80.0,
        "trunk" => 95.0,
        "trunk_link" => 75.0,
        "primary" => 80.0,
        "primary_link" => 60.0,
        "secondary" => 70.0,
        "secondary_link" => 55.0,
        "tertiary" => 55.0,
        "tertiary_link" => 45.0,
        "residential" | "unclassified" | "road" => 40.0,
        "living_street" => 15.0,
        "service" => 25.0,
        _ => 35.0,
    };

    ParsedSpeed {
        kph,
        source: MaxSpeedSource::HighwayDefault,
    }
}

fn parse_oneway(value: &str) -> Oneway {
    match value.trim().to_ascii_lowercase().as_str() {
        "yes" | "true" | "1" => Oneway::Forward,
        "-1" | "reverse" => Oneway::Reverse,
        _ => Oneway::No,
    }
}

fn infer_oneway(highway: &str, junction: Option<&str>, explicit_oneway: Option<&str>) -> Oneway {
    if let Some(value) = explicit_oneway {
        return parse_oneway(value);
    }

    if matches!(junction, Some("roundabout" | "circular")) {
        return Oneway::Forward;
    }

    if matches!(highway, "motorway" | "motorway_link") {
        return Oneway::Forward;
    }

    Oneway::No
}

fn is_supported_highway(highway: &str) -> bool {
    matches!(
        highway,
        "motorway"
            | "motorway_link"
            | "trunk"
            | "trunk_link"
            | "primary"
            | "primary_link"
            | "secondary"
            | "secondary_link"
            | "tertiary"
            | "tertiary_link"
            | "residential"
            | "living_street"
            | "unclassified"
            | "road"
            | "service"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn segment_crossing_bounds_is_included() {
        // Segment crosses the box horizontally but both endpoints are outside.
        assert!(segment_intersects_rect(
            LatLng::new(0.5, -2.0),
            LatLng::new(0.5, 2.0),
            Rect {
                min_x: -1.0,
                max_x: 1.0,
                min_y: 0.0,
                max_y: 1.0,
            },
        ));
    }

    #[test]
    fn dateline_crossing_segment_does_not_hit_greenwich_bounds() {
        assert!(!segment_intersects_rect(
            LatLng::new(0.0, 179.9),
            LatLng::new(0.0, -179.9),
            Rect {
                min_x: -1.0,
                max_x: 1.0,
                min_y: -1.0,
                max_y: 1.0,
            },
        ));
    }

    #[test]
    fn dateline_crossing_segment_hits_dateline_bounds() {
        assert!(segment_intersects_rect(
            LatLng::new(0.0, 179.9),
            LatLng::new(0.0, -179.9),
            Rect {
                min_x: 179.8,
                max_x: 180.0,
                min_y: -1.0,
                max_y: 1.0,
            },
        ));
        assert!(segment_intersects_rect(
            LatLng::new(0.0, 179.9),
            LatLng::new(0.0, -179.9),
            Rect {
                min_x: -180.0,
                max_x: -179.8,
                min_y: -1.0,
                max_y: 1.0,
            },
        ));
    }

    #[test]
    fn segment_completely_outside_is_excluded() {
        assert!(!segment_intersects_rect(
            LatLng::new(2.0, -2.0),
            LatLng::new(2.5, -1.5),
            Rect {
                min_x: -1.0,
                max_x: 1.0,
                min_y: 0.0,
                max_y: 1.0,
            },
        ));
    }

    #[test]
    fn edge_intersects_antimeridian_bounds_when_near_dateline() {
        let bounds = Bounds::new(-1.0, 170.0, 1.0, -170.0);
        assert!(edge_intersects_bounds(
            LatLng::new(0.0, 179.8),
            LatLng::new(0.0, 179.9),
            &bounds,
            0.0,
        ));
        assert!(edge_intersects_bounds(
            LatLng::new(0.0, -179.9),
            LatLng::new(0.0, -179.8),
            &bounds,
            0.0,
        ));
    }

    #[test]
    fn edge_outside_antimeridian_bounds_is_excluded() {
        let bounds = Bounds::new(-1.0, 170.0, 1.0, -170.0);
        assert!(!edge_intersects_bounds(
            LatLng::new(0.0, 0.0),
            LatLng::new(0.0, 0.1),
            &bounds,
            0.0,
        ));
    }

    #[test]
    fn edge_intersects_full_world_bounds() {
        let bounds = Bounds::new(-1.0, -180.0, 1.0, 180.0);
        assert!(edge_intersects_bounds(
            LatLng::new(0.0, 0.0),
            LatLng::new(0.0, 0.1),
            &bounds,
            0.0,
        ));
    }

    #[test]
    fn edge_intersects_when_buffer_expands_to_full_world() {
        let bounds = Bounds::new(-1.0, -179.0, 1.0, 179.0);
        assert!(edge_intersects_bounds(
            LatLng::new(0.0, 0.0),
            LatLng::new(0.0, 0.1),
            &bounds,
            200_000.0,
        ));
    }

    #[test]
    fn infer_oneway_uses_roundabout_default() {
        let oneway = infer_oneway("primary", Some("roundabout"), None);
        assert!(matches!(oneway, Oneway::Forward));
    }

    #[test]
    fn infer_oneway_explicit_tag_overrides_defaults() {
        let oneway = infer_oneway("motorway", Some("roundabout"), Some("no"));
        assert!(matches!(oneway, Oneway::No));
    }

    #[test]
    fn supported_highway_filters_out_footway() {
        assert!(is_supported_highway("primary"));
        assert!(is_supported_highway("motorway_link"));
        assert!(!is_supported_highway("footway"));
        assert!(!is_supported_highway("cycleway"));
    }

    #[test]
    fn parse_maxspeed_mph() {
        let parsed = parse_maxspeed_tag("35 mph").unwrap();
        assert!((parsed.kph - 56.327).abs() < 0.2);
        assert!(matches!(parsed.source, MaxSpeedSource::TagMph));
    }

    #[test]
    fn parse_maxspeed_zone() {
        let parsed = parse_maxspeed_tag("US:urban").unwrap();
        assert!((parsed.kph - 48.280).abs() < 0.2);
        assert!(matches!(parsed.source, MaxSpeedSource::ZoneTag));
    }

    #[test]
    fn resolve_edge_speed_prefers_directional_on_oneway() {
        let speed = resolve_edge_speed(
            "primary",
            Oneway::Forward,
            parse_maxspeed_tag("50"),
            parse_maxspeed_tag("40"),
            parse_maxspeed_tag("60"),
        );
        assert!((speed.nominal.kph - 40.0).abs() < 1e-9);
        assert!((speed.forward.kph - 40.0).abs() < 1e-9);
        assert!((speed.backward.kph - 40.0).abs() < 1e-9);
    }

    #[test]
    fn resolve_edge_speed_falls_back_to_highway_default() {
        let speed = resolve_edge_speed("residential", Oneway::No, None, None, None);
        assert!(matches!(
            speed.nominal.source,
            MaxSpeedSource::HighwayDefault
        ));
        assert!((speed.nominal.kph - 40.0).abs() < 1e-9);
    }

    #[test]
    fn resolve_edge_speed_keeps_two_way_directional_values() {
        let speed = resolve_edge_speed(
            "primary",
            Oneway::No,
            None,
            parse_maxspeed_tag("40"),
            parse_maxspeed_tag("60"),
        );
        assert!((speed.forward.kph - 40.0).abs() < 1e-9);
        assert!((speed.backward.kph - 60.0).abs() < 1e-9);
        assert!((speed.nominal.kph - 50.0).abs() < 1e-9);
    }
}
