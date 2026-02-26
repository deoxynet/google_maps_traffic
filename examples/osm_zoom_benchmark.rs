use google_maps_traffic::{Bounds, MatchConfig, MatchOutput, OsmRoadNetwork, TrafficClient};
use std::time::Instant;

#[derive(Clone, Copy)]
struct ZoomCase {
    name: &'static str,
    bounds: Bounds,
    zoom: u8,
}

fn percentile(values: &[f64], p: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let idx = ((sorted.len() - 1) as f64 * p).round() as usize;
    sorted[idx]
}

fn summarize(output: &MatchOutput, total_segments: usize) -> String {
    let matched = total_segments.saturating_sub(output.unmatched.len());
    let rate = (matched as f64) * 100.0 / (total_segments.max(1) as f64);

    let dists: Vec<f64> = output.matches.iter().map(|m| m.distance_m).collect();
    let bearings: Vec<f64> = output.matches.iter().map(|m| m.bearing_diff_deg).collect();

    format!(
        "matched={matched}/{total_segments} ({rate:.1}%) d95={:.1}m b95={:.1}deg",
        percentile(&dists, 0.95),
        percentile(&bearings, 0.95),
    )
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let pbf_path = std::env::args().nth(1).ok_or_else(|| {
        "usage: cargo run --example osm_zoom_benchmark --features osm -- <path/to.osm.pbf>"
            .to_string()
    })?;

    let cases = [
        ZoomCase {
            name: "z10_la_wide",
            bounds: Bounds::new(33.70, -118.55, 34.20, -117.80),
            zoom: 10,
        },
        ZoomCase {
            name: "z12_southbay",
            bounds: Bounds::new(33.740, -118.420, 33.930, -118.180),
            zoom: 12,
        },
        ZoomCase {
            name: "z14_dtla",
            bounds: Bounds::new(33.999, -118.265, 34.065, -118.225),
            zoom: 14,
        },
        ZoomCase {
            name: "z16_micro",
            bounds: Bounds::new(34.040, -118.255, 34.048, -118.245),
            zoom: 16,
        },
    ];

    let client = TrafficClient::new()?;

    for case in cases {
        println!("\n=== {} (z{}) ===", case.name, case.zoom);

        let fetch_start = Instant::now();
        let tiles = client.get_traffic(&case.bounds, case.zoom).await?;
        let fetch_ms = fetch_start.elapsed().as_millis();
        let total_segments: usize = tiles
            .iter()
            .flat_map(|t| &t.features)
            .map(|f| f.segments.len())
            .sum();
        println!(
            "traffic: tiles={} segments={} fetch_ms={fetch_ms}",
            tiles.len(),
            total_segments
        );

        let load_start = Instant::now();
        let network =
            OsmRoadNetwork::from_pbf_for_traffic_bounds(&pbf_path, &case.bounds, case.zoom)?;
        let load_ms = load_start.elapsed().as_millis();
        println!("osm: edges={} load_ms={load_ms}", network.edge_count());

        let cfg = MatchConfig::for_zoom(case.zoom);
        let match_start = Instant::now();
        let output = network.match_tiles(&tiles, &cfg);
        let match_ms = match_start.elapsed().as_millis();
        println!(
            "for_zoom: {} match_ms={match_ms} cfg=({}, {})",
            summarize(&output, total_segments),
            cfg.max_distance_m,
            cfg.max_bearing_diff_deg,
        );

        let relaxed = MatchConfig {
            max_distance_m: 80.0,
            max_bearing_diff_deg: 45.0,
            ..MatchConfig::default()
        };
        let relaxed_start = Instant::now();
        let relaxed_out = network.match_tiles(&tiles, &relaxed);
        let relaxed_ms = relaxed_start.elapsed().as_millis();
        println!(
            "relaxed: {} match_ms={relaxed_ms}",
            summarize(&relaxed_out, total_segments),
        );
    }

    Ok(())
}
