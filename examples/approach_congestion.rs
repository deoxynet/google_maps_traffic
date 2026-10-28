//! Fetch Google Maps typical traffic and match congestion to intersection approaches.
//!
//! Reads JSON from stdin describing intersections and a time window, fetches
//! typical traffic tiles at zoom 15, and for each intersection finds nearby
//! traffic features, matching them to approach directions (NB/SB/EB/WB) by
//! bearing. Outputs JSON with the worst congestion level per approach.
//!
//! Usage:
//!   echo '{"day":"Monday","hour":17,"intersections":[{"id":253,"lat":34.048,"lon":-118.257}]}' \
//!     | cargo run --release --example approach_congestion
//!
//! Input JSON:
//!   { "day": "Monday", "hour": 8,
//!     "intersections": [{"id": 123, "lat": 34.05, "lon": -118.25}, ...] }
//!
//! Output JSON (one line):
//!   [{"id":123,"congestion":{"NB":0,"SB":2,"EB":1,"WB":0}}, ...]
//!
//! Congestion levels: 0=Normal, 1=Slow, 2=Heavy, 3=Severe

use google_maps_traffic::{
    CongestionLevel, DayOfWeek, LatLng, TileCoord, TrafficClient, TrafficFeature,
};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::io::Read;

const ZOOM: u8 = 15;
const MATCH_RADIUS_M: f64 = 60.0;
const BEARING_TOLERANCE_DEG: f64 = 45.0;

// Approximate meters per degree at LA latitude
const M_PER_DEG_LAT: f64 = 111_139.0;
const M_PER_DEG_LNG_LA: f64 = 92_300.0; // cos(34°) * 111139

#[derive(Deserialize)]
struct Input {
    day: String,
    hour: u8,
    intersections: Vec<IntersectionInput>,
}

#[derive(Deserialize)]
struct IntersectionInput {
    id: i64,
    lat: f64,
    lon: f64,
}

#[derive(Serialize)]
struct IntersectionOutput {
    id: i64,
    congestion: HashMap<String, u8>,
}

fn parse_day(s: &str) -> Result<DayOfWeek, Box<dyn Error>> {
    match s.to_lowercase().as_str() {
        "sunday" | "sun" => Ok(DayOfWeek::Sunday),
        "monday" | "mon" => Ok(DayOfWeek::Monday),
        "tuesday" | "tue" => Ok(DayOfWeek::Tuesday),
        "wednesday" | "wed" => Ok(DayOfWeek::Wednesday),
        "thursday" | "thu" => Ok(DayOfWeek::Thursday),
        "friday" | "fri" => Ok(DayOfWeek::Friday),
        "saturday" | "sat" => Ok(DayOfWeek::Saturday),
        _ => Err(format!("unknown day: {s}").into()),
    }
}

fn congestion_to_u8(c: &CongestionLevel) -> u8 {
    match c {
        CongestionLevel::Normal => 0,
        CongestionLevel::Slow => 1,
        CongestionLevel::Heavy => 2,
        CongestionLevel::Severe => 3,
        CongestionLevel::Unknown(_) => 0,
    }
}

/// Map a feature bearing to an approach direction.
/// A feature heading ~0° (north) represents traffic flowing northbound (NB).
/// Returns None if bearing doesn't match any cardinal direction within tolerance.
fn bearing_to_approach(bearing: f64) -> Option<&'static str> {
    let dirs: [(&str, f64); 4] = [("NB", 0.0), ("EB", 90.0), ("SB", 180.0), ("WB", 270.0)];
    for (name, target) in &dirs {
        let mut diff = (bearing - target).abs();
        if diff > 180.0 {
            diff = 360.0 - diff;
        }
        if diff <= BEARING_TOLERANCE_DEG {
            return Some(name);
        }
    }
    None
}

/// Haversine-like distance in meters between two points (flat-earth approx for short distances).
fn distance_m(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    let dlat = (lat2 - lat1) * M_PER_DEG_LAT;
    let dlon = (lon2 - lon1) * M_PER_DEG_LNG_LA;
    (dlat * dlat + dlon * dlon).sqrt()
}

/// Find the closest point on a feature's geometry to the intersection.
fn min_distance_to_feature(feat: &TrafficFeature, lat: f64, lon: f64) -> f64 {
    feat.geometry
        .iter()
        .map(|p| distance_m(lat, lon, p.lat, p.lng))
        .fold(f64::INFINITY, f64::min)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    // Read input from stdin
    let mut input_str = String::new();
    std::io::stdin().read_to_string(&mut input_str)?;
    let input: Input = serde_json::from_str(&input_str)?;

    let day = parse_day(&input.day)?;
    let hour = input.hour;

    if input.intersections.is_empty() {
        println!("[]");
        return Ok(());
    }

    eprintln!(
        "fetching typical {} {:02}:00 traffic for {} intersections...",
        input.day,
        hour,
        input.intersections.len()
    );

    // Group intersections by zoom-15 tile to deduplicate tile fetches
    let mut tile_set: HashSet<(u32, u32)> = HashSet::new();
    for ix in &input.intersections {
        let coord = TileCoord::from_latlng(&LatLng::new(ix.lat, ix.lon), ZOOM)?;
        // Also include neighboring tiles for intersections near tile edges
        for dx in -1i32..=1 {
            for dy in -1i32..=1 {
                let nx = coord.x as i32 + dx;
                let ny = coord.y as i32 + dy;
                let max = 1i32 << ZOOM;
                if nx >= 0 && nx < max && ny >= 0 && ny < max {
                    tile_set.insert((nx as u32, ny as u32));
                }
            }
        }
    }

    let coords: Vec<TileCoord> = tile_set
        .iter()
        .map(|&(x, y)| TileCoord { zoom: ZOOM, x, y })
        .collect();

    eprintln!("fetching {} tiles at zoom {}...", coords.len(), ZOOM);

    let client = TrafficClient::new()?;
    let tiles = client.get_typical_tiles(&coords, day, hour).await?;

    // Collect all features across tiles
    let all_features: Vec<&TrafficFeature> = tiles.iter().flat_map(|t| &t.features).collect();
    eprintln!(
        "{} features across {} tiles",
        all_features.len(),
        tiles.len()
    );

    // Match features to intersections
    let mut results: Vec<IntersectionOutput> = Vec::with_capacity(input.intersections.len());

    for ix in &input.intersections {
        let mut approach_congestion: HashMap<String, u8> = HashMap::new();

        for feat in &all_features {
            let dist = min_distance_to_feature(feat, ix.lat, ix.lon);
            if dist > MATCH_RADIUS_M {
                continue;
            }

            if let Some(approach) = bearing_to_approach(feat.bearing) {
                // Take the worst (max) congestion level per segment near the intersection
                let worst = feat
                    .segments
                    .iter()
                    .map(|s| congestion_to_u8(&s.congestion))
                    .max()
                    .unwrap_or(0);

                let entry = approach_congestion.entry(approach.to_string()).or_insert(0);
                *entry = (*entry).max(worst);
            }
        }

        results.push(IntersectionOutput {
            id: ix.id,
            congestion: approach_congestion,
        });
    }

    let matched = results.iter().filter(|r| !r.congestion.is_empty()).count();
    eprintln!(
        "matched {}/{} intersections with congestion data",
        matched,
        results.len()
    );

    // Output JSON
    let json = serde_json::to_string(&results)?;
    println!("{json}");

    Ok(())
}
