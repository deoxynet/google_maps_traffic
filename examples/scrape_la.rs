//! Scrape live Google Maps traffic for the greater LA metro area.
//!
//! Fetches all tiles at the configured zoom level, serializes to compact
//! gzipped JSON, and writes to a file.  Designed to run from CI every
//! few minutes with the output uploaded to Cloudflare R2.
//!
//! Usage:
//!   cargo run --release --example scrape_la
//!
//! Environment:
//!   ZOOM=15          Tile zoom level (default: 15)
//!   OUTPUT=path      Output file (default: snapshot.json.gz)

use flate2::write::GzEncoder;
use flate2::Compression;
use google_maps_traffic::{Bounds, CongestionLevel, IncidentType, TrafficClient, TileCoord};
use serde::Serialize;
use std::io::Write;
use std::time::{SystemTime, UNIX_EPOCH};

/// Greater LA metro bounds — covers LAX to Pasadena, Long Beach to SFV.
const LA_BOUNDS: Bounds = Bounds {
    south: 33.70,
    west: -118.70,
    north: 34.35,
    east: -117.75,
};

const DEFAULT_ZOOM: u8 = 15;

#[derive(Serialize)]
struct Snapshot {
    /// Unix timestamp (seconds)
    timestamp: u64,
    /// ISO 8601 UTC string
    datetime: String,
    zoom: u8,
    bounds: [f64; 4],
    tile_count: usize,
    summary: Summary,
    features: Vec<Feature>,
    incidents: Vec<Incident>,
}

#[derive(Serialize)]
struct Summary {
    total_features: usize,
    total_segments: usize,
    total_geometry_points: usize,
    normal: usize,
    slow: usize,
    heavy: usize,
    severe: usize,
}

#[derive(Serialize)]
struct Feature {
    /// Geometry as [[lat, lng], ...] rounded to 5 decimal places (~1m precision)
    geometry: Vec<[f64; 2]>,
    /// Bearing in degrees (0=N, 90=E, 180=S, 270=W)
    bearing: f64,
    /// Road segments with congestion levels
    segments: Vec<Segment>,
}

#[derive(Serialize)]
struct Segment {
    /// Start index into geometry array
    start: usize,
    /// End index into geometry array
    end: usize,
    /// Congestion level: 0=normal/green, 1=slow/yellow, 2=heavy/red, 3=severe/dark-red
    congestion: u8,
}

#[derive(Serialize)]
struct Incident {
    /// "accident", "road_closure", "construction", or "other"
    incident_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    description: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    short_description: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    lat: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    lon: Option<f64>,
}

fn congestion_u8(c: CongestionLevel) -> u8 {
    match c {
        CongestionLevel::Normal => 0,
        CongestionLevel::Slow => 1,
        CongestionLevel::Heavy => 2,
        CongestionLevel::Severe => 3,
        CongestionLevel::Unknown(v) => v,
    }
}

fn incident_type_str(t: &IncidentType) -> &'static str {
    match t {
        IncidentType::Accident => "accident",
        IncidentType::RoadClosure => "road_closure",
        IncidentType::Construction => "construction",
        IncidentType::Other(_) => "other",
    }
}

fn round5(v: f64) -> f64 {
    (v * 100_000.0).round() / 100_000.0
}

/// Format Unix timestamp as ISO 8601 UTC.
fn unix_to_iso(secs: u64) -> String {
    // Civil date from Unix days — Howard Hinnant's algorithm
    let days = (secs / 86400) as u64;
    let time_of_day = secs % 86400;
    let h = time_of_day / 3600;
    let m = (time_of_day % 3600) / 60;
    let s = time_of_day % 60;

    let z = days + 719468;
    let era = z / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mo = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if mo <= 2 { y + 1 } else { y };

    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{m:02}:{s:02}Z")
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let zoom: u8 = std::env::var("ZOOM")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(DEFAULT_ZOOM);

    let output = std::env::var("OUTPUT").unwrap_or_else(|_| "snapshot.json.gz".into());

    let coords = TileCoord::tiles_for_bounds(&LA_BOUNDS, zoom)?;
    eprintln!("Fetching {} tiles at zoom {} for greater LA...", coords.len(), zoom);

    let client = TrafficClient::new()?;
    let start = std::time::Instant::now();
    let tiles = client.get_traffic(&LA_BOUNDS, zoom).await?;
    let fetch_secs = start.elapsed().as_secs_f64();

    // Build features
    let mut features = Vec::new();
    let mut incidents = Vec::new();
    let mut summary = Summary {
        total_features: 0,
        total_segments: 0,
        total_geometry_points: 0,
        normal: 0,
        slow: 0,
        heavy: 0,
        severe: 0,
    };

    for tile in &tiles {
        for feat in &tile.features {
            if feat.geometry.len() < 2 || feat.segments.is_empty() {
                continue;
            }

            let geometry: Vec<[f64; 2]> = feat
                .geometry
                .iter()
                .map(|p| [round5(p.lat), round5(p.lng)])
                .collect();

            let segments: Vec<Segment> = feat
                .segments
                .iter()
                .map(|seg| {
                    let c = congestion_u8(seg.congestion);
                    match c {
                        0 => summary.normal += 1,
                        1 => summary.slow += 1,
                        2 => summary.heavy += 1,
                        3 => summary.severe += 1,
                        _ => {}
                    }
                    summary.total_segments += 1;
                    Segment {
                        start: seg.start_index,
                        end: seg.end_index,
                        congestion: c,
                    }
                })
                .collect();

            summary.total_geometry_points += geometry.len();
            summary.total_features += 1;

            features.push(Feature {
                geometry,
                bearing: (feat.bearing * 10.0).round() / 10.0,
                segments,
            });
        }

        for inc in &tile.incidents {
            incidents.push(Incident {
                incident_type: incident_type_str(&inc.incident_type).to_string(),
                description: inc.description.clone(),
                short_description: inc.short_description.clone(),
                lat: inc.location.map(|l| round5(l.lat)),
                lon: inc.location.map(|l| round5(l.lng)),
            });
        }
    }

    // Deduplicate incidents by description+location (tiles can overlap)
    incidents.sort_by(|a, b| {
        a.description
            .cmp(&b.description)
            .then(a.lat.map(|v| v.to_bits()).cmp(&b.lat.map(|v| v.to_bits())))
    });
    incidents.dedup_by(|a, b| {
        a.description == b.description && a.lat == b.lat && a.lon == b.lon
    });

    let now_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();

    let snapshot = Snapshot {
        timestamp: now_secs,
        datetime: unix_to_iso(now_secs),
        zoom,
        bounds: [LA_BOUNDS.south, LA_BOUNDS.west, LA_BOUNDS.north, LA_BOUNDS.east],
        tile_count: tiles.len(),
        summary,
        features,
        incidents,
    };

    // Serialize and gzip
    let json = serde_json::to_vec(&snapshot)?;
    let raw_size = json.len();

    let file = std::fs::File::create(&output)?;
    let mut gz = GzEncoder::new(file, Compression::default());
    gz.write_all(&json)?;
    gz.finish()?;

    let gz_size = std::fs::metadata(&output)?.len();

    eprintln!(
        "Done in {fetch_secs:.1}s: {} features, {} segments, {} incidents",
        snapshot.summary.total_features,
        snapshot.summary.total_segments,
        snapshot.incidents.len(),
    );
    eprintln!(
        "Congestion: {} normal, {} slow, {} heavy, {} severe",
        snapshot.summary.normal,
        snapshot.summary.slow,
        snapshot.summary.heavy,
        snapshot.summary.severe,
    );
    eprintln!(
        "Output: {} ({:.1} MB raw → {:.1} MB gzipped, {:.0}:1)",
        output,
        raw_size as f64 / 1_048_576.0,
        gz_size as f64 / 1_048_576.0,
        raw_size as f64 / gz_size as f64,
    );

    // Print R2 key path to stdout for the CI script to capture
    let date = &snapshot.datetime[..10]; // YYYY-MM-DD
    let hour = &snapshot.datetime[11..13]; // HH
    let minsec = &snapshot.datetime[14..16]; // MM (skip seconds for path)
    println!("live/{date}/{hour}/{minsec}.json.gz");

    Ok(())
}
