//! Scrape live Google Maps traffic for the greater LA metro area.
//!
//! Fetches tiles at multiple zoom levels, serializes to compact gzipped JSON,
//! and writes one file per zoom.  Designed to run from CI every few minutes
//! with outputs uploaded to Cloudflare R2.
//!
//! Zoom coverage:
//!   12 — ~120 tiles, freeways only
//!   13 — ~437 tiles, freeways + major highways
//!   14 — ~1,665 tiles, adds major arterials
//!   15 — ~6,336 tiles, full street-level detail
//!
//! Usage:
//!   cargo run --release --example scrape_la
//!
//! Environment:
//!   ZOOMS=12,13,14,15   Comma-separated zoom levels (default: all four)
//!   OUTPUT_DIR=.         Directory for output files (default: current dir)

use flate2::Compression;
use flate2::write::GzEncoder;
use google_maps_traffic::{Bounds, CongestionLevel, IncidentType, TileCoord, TrafficClient};
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

const DEFAULT_ZOOMS: &[u8] = &[12, 13, 14, 15];

#[derive(Serialize)]
struct Snapshot {
    timestamp: u64,
    datetime: String,
    zoom: u8,
    bounds: [f64; 4],
    tile_count: usize,
    summary: Summary,
    features: Vec<Feature>,
    incidents: Vec<Incident>,
}

#[derive(Serialize, Default)]
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
    geometry: Vec<[f64; 2]>,
    bearing: f64,
    segments: Vec<Segment>,
}

#[derive(Serialize)]
struct Segment {
    start: usize,
    end: usize,
    congestion: u8,
}

#[derive(Serialize)]
struct Incident {
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

fn unix_to_iso(secs: u64) -> String {
    let days = secs / 86400;
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

async fn scrape_zoom(
    client: &TrafficClient,
    zoom: u8,
    output_dir: &str,
    now_secs: u64,
    datetime: &str,
) -> Result<(String, String), Box<dyn std::error::Error>> {
    let coords = TileCoord::tiles_for_bounds(&LA_BOUNDS, zoom)?;
    eprintln!("  z{zoom}: fetching {} tiles...", coords.len());

    let start = std::time::Instant::now();
    let tiles = client.get_traffic(&LA_BOUNDS, zoom).await?;
    let fetch_secs = start.elapsed().as_secs_f64();

    let mut features = Vec::new();
    let mut incidents = Vec::new();
    let mut summary = Summary::default();

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

    incidents.sort_by(|a, b| {
        a.description
            .cmp(&b.description)
            .then(a.lat.map(|v| v.to_bits()).cmp(&b.lat.map(|v| v.to_bits())))
    });
    incidents.dedup_by(|a, b| a.description == b.description && a.lat == b.lat && a.lon == b.lon);

    let snapshot = Snapshot {
        timestamp: now_secs,
        datetime: datetime.to_string(),
        zoom,
        bounds: [
            LA_BOUNDS.south,
            LA_BOUNDS.west,
            LA_BOUNDS.north,
            LA_BOUNDS.east,
        ],
        tile_count: tiles.len(),
        summary,
        features,
        incidents,
    };

    let filename = format!("snapshot_z{zoom}.json.gz");
    let filepath = if output_dir == "." {
        filename.clone()
    } else {
        format!("{output_dir}/{filename}")
    };

    let json = serde_json::to_vec(&snapshot)?;
    let raw_size = json.len();

    let file = std::fs::File::create(&filepath)?;
    let mut gz = GzEncoder::new(file, Compression::default());
    gz.write_all(&json)?;
    gz.finish()?;

    let gz_size = std::fs::metadata(&filepath)?.len();

    eprintln!(
        "  z{zoom}: {:.1}s — {} feat, {} seg (N:{} S:{} H:{} X:{}) — {:.1}MB → {:.1}MB gz",
        fetch_secs,
        snapshot.summary.total_features,
        snapshot.summary.total_segments,
        snapshot.summary.normal,
        snapshot.summary.slow,
        snapshot.summary.heavy,
        snapshot.summary.severe,
        raw_size as f64 / 1_048_576.0,
        gz_size as f64 / 1_048_576.0,
    );

    let date = &datetime[..10];
    let hour = &datetime[11..13];
    let min = &datetime[14..16];
    let r2_key = format!("z{zoom}/{date}/{hour}/{min}.json.gz");

    Ok((filepath, r2_key))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let zooms: Vec<u8> = std::env::var("ZOOMS")
        .ok()
        .map(|s| s.split(',').filter_map(|v| v.trim().parse().ok()).collect())
        .unwrap_or_else(|| DEFAULT_ZOOMS.to_vec());

    let output_dir = std::env::var("OUTPUT_DIR").unwrap_or_else(|_| ".".into());

    if output_dir != "." {
        std::fs::create_dir_all(&output_dir)?;
    }

    let now_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let datetime = unix_to_iso(now_secs);

    eprintln!("Scraping LA traffic at {} — zooms {:?}", datetime, zooms);

    let client = TrafficClient::new()?;
    let total_start = std::time::Instant::now();

    // Output: one line per zoom with "filepath r2_key" for the CI upload step
    for &zoom in &zooms {
        let (filepath, r2_key) =
            scrape_zoom(&client, zoom, &output_dir, now_secs, &datetime).await?;
        println!("{filepath} {r2_key}");
    }

    eprintln!("Total: {:.1}s", total_start.elapsed().as_secs_f64());

    Ok(())
}
