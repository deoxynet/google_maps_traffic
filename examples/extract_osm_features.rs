//! Extract traffic control (signals, stops) and lane counts from OSM PBF.
//!
//! Two-pass PBF reading:
//!   Pass 1 — Ways: collect highway ways with node_refs and lanes tags
//!   Pass 2 — Nodes: resolve lat/lon, detect highway=traffic_signals and highway=stop
//!
//! Outputs `data/osm_features.json` for use by `scripts/enrich_osm_features.py`.
//!
//! Usage:
//!   cargo run --release --example extract_osm_features --features osm -- socal.osm.pbf

use osmpbf::{Element, ElementReader};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::io::{BufWriter, Write};
use std::path::Path;

#[derive(Serialize)]
struct Output {
    signal_nodes: Vec<[f64; 2]>,
    stop_nodes: Vec<[f64; 2]>,
    way_segments: Vec<WaySegment>,
    parking: Vec<ParkingFeature>,
}

#[derive(Serialize)]
struct WaySegment {
    start: [f64; 2],
    end: [f64; 2],
    lanes: u8,
    highway: String,
}

#[derive(Serialize)]
struct ParkingFeature {
    lat: f64,
    lon: f64,
    kind: String,    // surface, underground, multi-storey, street_side, lane, etc.
    capacity: u32,   // from capacity tag, or estimated from area
}

struct WaySpec {
    highway: String,
    node_refs: Vec<i64>,
    lanes: Option<u8>,
}

struct ParkingWaySpec {
    kind: String,
    capacity: Option<u32>,
    node_refs: Vec<i64>,
}

struct BuildingWaySpec {
    node_refs: Vec<i64>,
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

fn parse_lanes(value: &str) -> Option<u8> {
    value
        .trim()
        .split(';')
        .next()?
        .trim()
        .parse::<u8>()
        .ok()
        .filter(|&n| n >= 1 && n <= 12)
}

fn main() -> Result<(), Box<dyn Error>> {
    let pbf_path = std::env::args().nth(1).ok_or_else(|| {
        "usage: cargo run --release --example extract_osm_features --features osm -- <path.osm.pbf>"
            .to_string()
    })?;
    let output_path = std::env::var("OUTPUT").unwrap_or_else(|_| "data/osm_features.json".into());

    // ── Pass 1: Collect highway ways + parking ways ─────────────────
    println!("Pass 1: Reading ways from {pbf_path}...");
    let mut ways: Vec<WaySpec> = Vec::new();
    let mut parking_ways: Vec<ParkingWaySpec> = Vec::new();
    let mut building_ways: Vec<BuildingWaySpec> = Vec::new();
    let mut needed_node_ids: HashSet<i64> = HashSet::new();

    let reader = ElementReader::from_path(&pbf_path)?;
    reader.for_each(|element| {
        if let Element::Way(way) = element {
            let mut highway: Option<String> = None;
            let mut lanes_tag: Option<&str> = None;
            let mut lanes_fwd: Option<&str> = None;
            let mut lanes_bwd: Option<&str> = None;
            let mut amenity: Option<String> = None;
            let mut parking_tag: Option<String> = None;
            let mut capacity_tag: Option<&str> = None;
            let mut building_tag: Option<String> = None;

            for (key, value) in way.tags() {
                match key {
                    "highway" => highway = Some(value.to_ascii_lowercase()),
                    "lanes" => lanes_tag = Some(value),
                    "lanes:forward" => lanes_fwd = Some(value),
                    "lanes:backward" => lanes_bwd = Some(value),
                    "amenity" => amenity = Some(value.to_ascii_lowercase()),
                    "parking" => parking_tag = Some(value.to_ascii_lowercase()),
                    "capacity" => capacity_tag = Some(value),
                    "building" => building_tag = Some(value.to_ascii_lowercase()),
                    _ => {}
                }
            }

            // Building way (building=* except "no")
            if let Some(ref bval) = building_tag {
                if bval != "no" {
                    let refs: Vec<i64> = way.refs().collect();
                    if refs.len() >= 3 {
                        for &nid in &refs {
                            needed_node_ids.insert(nid);
                        }
                        building_ways.push(BuildingWaySpec { node_refs: refs });
                    }
                    return;
                }
            }

            // Parking way (amenity=parking)
            if amenity.as_deref() == Some("parking") {
                let refs: Vec<i64> = way.refs().collect();
                if refs.len() >= 2 {
                    let kind = parking_tag.unwrap_or_else(|| "surface".into());
                    let capacity = capacity_tag.and_then(|v| v.trim().parse::<u32>().ok());
                    for &nid in &refs {
                        needed_node_ids.insert(nid);
                    }
                    parking_ways.push(ParkingWaySpec {
                        kind,
                        capacity,
                        node_refs: refs,
                    });
                }
                return;
            }

            let Some(highway) = highway else { return };
            if !is_supported_highway(&highway) {
                return;
            }

            let refs: Vec<i64> = way.refs().collect();
            if refs.len() < 2 {
                return;
            }

            // Parse lanes: prefer directional sum, fall back to total
            let lanes = if let (Some(f), Some(b)) = (lanes_fwd, lanes_bwd) {
                match (parse_lanes(f), parse_lanes(b)) {
                    (Some(fl), Some(bl)) => Some(fl.saturating_add(bl)),
                    (Some(fl), None) => Some(fl),
                    (None, Some(bl)) => Some(bl),
                    _ => lanes_tag.and_then(parse_lanes),
                }
            } else {
                lanes_tag.and_then(parse_lanes)
            };

            for &nid in &refs {
                needed_node_ids.insert(nid);
            }

            ways.push(WaySpec {
                highway,
                node_refs: refs,
                lanes,
            });
        }
    })?;

    println!(
        "  {} ways, {} parking ways, {} building ways, {} unique node refs",
        ways.len(),
        parking_ways.len(),
        building_ways.len(),
        needed_node_ids.len()
    );

    // ── Pass 2: Resolve node positions + detect signal/stop/parking nodes
    println!("Pass 2: Reading nodes...");
    let mut node_pos: HashMap<i64, (f64, f64)> = HashMap::with_capacity(needed_node_ids.len());
    let mut signal_node_ids: HashSet<i64> = HashSet::new();
    let mut stop_node_ids: HashSet<i64> = HashSet::new();
    let mut parking_point_features: Vec<ParkingFeature> = Vec::new();

    let reader = ElementReader::from_path(&pbf_path)?;
    reader.for_each(|element| {
        let (id, lat, lon, tags_iter): (i64, f64, f64, Box<dyn Iterator<Item = (&str, &str)>>) =
            match element {
                Element::Node(ref node) => (
                    node.id(),
                    node.lat(),
                    node.lon(),
                    Box::new(node.tags()),
                ),
                Element::DenseNode(ref node) => (
                    node.id(),
                    node.lat(),
                    node.lon(),
                    Box::new(node.tags()),
                ),
                _ => return,
            };

        // Always resolve position if needed for ways
        if needed_node_ids.contains(&id) {
            node_pos.insert(id, (lat, lon));
        }

        // Check tags on ALL nodes
        let mut is_signal = false;
        let mut is_stop = false;
        let mut is_parking = false;
        let mut parking_kind: Option<String> = None;
        let mut capacity_val: Option<u32> = None;

        for (key, value) in tags_iter {
            match key {
                "highway" => {
                    let v = value.to_ascii_lowercase();
                    if v == "traffic_signals" {
                        is_signal = true;
                    } else if v == "stop" {
                        is_stop = true;
                    }
                }
                "amenity" if value.eq_ignore_ascii_case("parking") => {
                    is_parking = true;
                }
                "parking" => {
                    parking_kind = Some(value.to_ascii_lowercase());
                }
                "capacity" => {
                    capacity_val = value.trim().parse::<u32>().ok();
                }
                _ => {}
            }
        }

        if is_signal {
            signal_node_ids.insert(id);
        }
        if is_stop {
            stop_node_ids.insert(id);
        }
        if is_parking {
            parking_point_features.push(ParkingFeature {
                lat,
                lon,
                kind: parking_kind.unwrap_or_else(|| "surface".into()),
                capacity: capacity_val.unwrap_or(20),
            });
        }
    })?;

    println!(
        "  {} node positions resolved",
        node_pos.len()
    );
    println!(
        "  {} signal nodes, {} stop nodes, {} parking nodes",
        signal_node_ids.len(),
        stop_node_ids.len(),
        parking_point_features.len()
    );

    // ── Build parking features from ways (centroid + area-based capacity) ──
    let mut parking_way_features: Vec<ParkingFeature> = Vec::new();
    for pw in &parking_ways {
        let mut sum_lat = 0.0f64;
        let mut sum_lon = 0.0f64;
        let mut resolved = 0u32;
        let mut coords: Vec<(f64, f64)> = Vec::new();
        for &nid in &pw.node_refs {
            if let Some(&(lat, lon)) = node_pos.get(&nid) {
                sum_lat += lat;
                sum_lon += lon;
                coords.push((lat, lon));
                resolved += 1;
            }
        }
        if resolved < 2 {
            continue;
        }
        let centroid_lat = sum_lat / resolved as f64;
        let centroid_lon = sum_lon / resolved as f64;

        let capacity = if let Some(c) = pw.capacity {
            c
        } else {
            // Estimate from polygon area: ~25 sqm per parking space
            // Shoelace formula in approximate meters
            let lat_m = 111_320.0;
            let lon_m = 92_400.0;
            let n = coords.len();
            let mut area = 0.0f64;
            for i in 0..n {
                let j = (i + 1) % n;
                let xi = coords[i].1 * lon_m;
                let yi = coords[i].0 * lat_m;
                let xj = coords[j].1 * lon_m;
                let yj = coords[j].0 * lat_m;
                area += xi * yj - xj * yi;
            }
            let area_sqm = area.abs() / 2.0;
            // ~25 sqm per space for surface, ~15 for multi-storey per level
            let sqm_per_space = match pw.kind.as_str() {
                "multi-storey" | "underground" => 15.0,
                _ => 25.0,
            };
            let est = (area_sqm / sqm_per_space).max(1.0) as u32;
            // Multi-storey: estimate 4 levels
            if pw.kind == "multi-storey" { est * 4 } else { est }
        };

        parking_way_features.push(ParkingFeature {
            lat: centroid_lat,
            lon: centroid_lon,
            kind: pw.kind.clone(),
            capacity,
        });
    }

    // Combine point + way parking features
    let mut parking = parking_point_features;
    parking.extend(parking_way_features);
    println!(
        "  {} total parking features ({} from nodes, {} from ways)",
        parking.len(),
        parking.len() - parking_ways.len().min(parking.len()),
        parking_ways.len()
    );

    // ── Build output ────────────────────────────────────────────────

    // Signal/stop node coordinates
    let signal_nodes: Vec<[f64; 2]> = signal_node_ids
        .iter()
        .filter_map(|id| node_pos.get(id).map(|&(lat, lon)| [lat, lon]))
        .collect();

    let stop_nodes: Vec<[f64; 2]> = stop_node_ids
        .iter()
        .filter_map(|id| node_pos.get(id).map(|&(lat, lon)| [lat, lon]))
        .collect();

    // Way segments (only those with lanes data)
    let mut way_segments: Vec<WaySegment> = Vec::new();
    let mut segments_with_lanes = 0u64;
    let mut segments_total = 0u64;

    for way in &ways {
        let Some(lanes) = way.lanes else { continue };
        for pair in way.node_refs.windows(2) {
            segments_total += 1;
            let Some(&(lat0, lon0)) = node_pos.get(&pair[0]) else {
                continue;
            };
            let Some(&(lat1, lon1)) = node_pos.get(&pair[1]) else {
                continue;
            };
            way_segments.push(WaySegment {
                start: [lat0, lon0],
                end: [lat1, lon1],
                lanes,
                highway: way.highway.clone(),
            });
            segments_with_lanes += 1;
        }
    }

    println!(
        "  {} way segments with lanes data (of {} total in lanes-tagged ways)",
        segments_with_lanes, segments_total
    );

    // ── Write output ────────────────────────────────────────────────
    let output = Output {
        signal_nodes,
        stop_nodes,
        way_segments,
        parking,
    };

    if let Some(p) = Path::new(&output_path)
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
    {
        std::fs::create_dir_all(p)?;
    }

    let json = serde_json::to_string(&output)?;
    std::fs::write(&output_path, &json)?;
    println!(
        "\nWrote {} ({:.0} KB)",
        output_path,
        json.len() as f64 / 1024.0
    );
    println!("  {} signal nodes", output.signal_nodes.len());
    println!("  {} stop nodes", output.stop_nodes.len());
    println!("  {} way segments", output.way_segments.len());
    println!("  {} parking features", output.parking.len());

    // ── Write building polygons as NDJSON ────────────────────────
    let buildings_path = Path::new(&output_path)
        .parent()
        .unwrap_or(Path::new("data"))
        .join("osm_buildings.jsonl");
    if let Some(p) = buildings_path.parent() {
        std::fs::create_dir_all(p)?;
    }
    let bfile = std::fs::File::create(&buildings_path)?;
    let mut bwriter = BufWriter::new(bfile);
    let mut building_count = 0u64;
    for bw in &building_ways {
        let coords: Vec<[f64; 2]> = bw
            .node_refs
            .iter()
            .filter_map(|nid| node_pos.get(nid).map(|&(lat, lon)| [lat, lon]))
            .collect();
        if coords.len() < 3 {
            continue;
        }
        let line = serde_json::to_string(&coords)?;
        bwriter.write_all(line.as_bytes())?;
        bwriter.write_all(b"\n")?;
        building_count += 1;
    }
    bwriter.flush()?;
    println!(
        "\nWrote {} ({} buildings)",
        buildings_path.display(),
        building_count
    );

    // ── Write ALL road segments as NDJSON (with default lanes) ──
    let roads_path = Path::new(&output_path)
        .parent()
        .unwrap_or(Path::new("data"))
        .join("osm_roads.jsonl");
    let rfile = std::fs::File::create(&roads_path)?;
    let mut rwriter = BufWriter::new(rfile);
    let mut road_seg_count = 0u64;
    let mut defaulted_count = 0u64;

    for way in &ways {
        let lanes = way.lanes.unwrap_or_else(|| {
            defaulted_count += 1;
            match way.highway.as_str() {
                "motorway" | "trunk" | "primary" => 4,
                "motorway_link" | "trunk_link" | "primary_link" => 2,
                "secondary" | "secondary_link" | "tertiary" | "tertiary_link" => 2,
                "residential" | "unclassified" | "road" => 2,
                "service" | "living_street" => 1,
                _ => 2,
            }
        });
        for pair in way.node_refs.windows(2) {
            let Some(&(lat0, lon0)) = node_pos.get(&pair[0]) else {
                continue;
            };
            let Some(&(lat1, lon1)) = node_pos.get(&pair[1]) else {
                continue;
            };
            // Compact JSON: s=start, e=end, l=lanes, h=highway
            write!(
                rwriter,
                "{{\"s\":[{},{}],\"e\":[{},{}],\"l\":{},\"h\":\"{}\"}}\n",
                lat0, lon0, lat1, lon1, lanes, way.highway
            )?;
            road_seg_count += 1;
        }
    }
    rwriter.flush()?;
    println!(
        "\nWrote {} ({} road segments, {} with default lanes)",
        roads_path.display(),
        road_seg_count,
        defaulted_count
    );

    Ok(())
}
