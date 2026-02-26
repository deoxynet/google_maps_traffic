use google_maps_traffic::{Bounds, MatchConfig, OsmRoadNetwork, TrafficClient};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[derive(Clone, Copy)]
struct ZoomCase {
    name: &'static str,
    bounds: Bounds,
    zoom: u8,
}

fn parse_u32(arg: Option<&str>, flag: &str, default: u32) -> Result<u32, String> {
    match arg {
        Some(v) => v
            .parse::<u32>()
            .map_err(|_| format!("invalid value for {flag}: {v}")),
        None => Ok(default),
    }
}

fn parse_f64(arg: Option<&str>, flag: &str, default: f64) -> Result<f64, String> {
    match arg {
        Some(v) => v
            .parse::<f64>()
            .map_err(|_| format!("invalid value for {flag}: {v}")),
        None => Ok(default),
    }
}

fn utc_timestamp_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let pbf_path = args.next().ok_or_else(|| {
        "usage: cargo run --example osm_stress --features osm -- \
         <path/to.osm.pbf> [--iterations N] [--sleep-seconds S] [--max-drop-pp D] [--include-z10]"
            .to_string()
    })?;

    let mut iterations = 4u32;
    let mut sleep_seconds = 15u32;
    let mut max_drop_pp = 6.0f64;
    let mut include_z10 = false;

    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--iterations" => {
                iterations = parse_u32(args.next().as_deref(), "--iterations", iterations)?.max(1)
            }
            "--sleep-seconds" => {
                sleep_seconds = parse_u32(args.next().as_deref(), "--sleep-seconds", sleep_seconds)?
            }
            "--max-drop-pp" => {
                max_drop_pp = parse_f64(args.next().as_deref(), "--max-drop-pp", max_drop_pp)?
            }
            "--include-z10" => include_z10 = true,
            other => {
                return Err(format!("unknown argument: {other}").into());
            }
        }
    }

    let mut cases = Vec::new();
    if include_z10 {
        cases.push(ZoomCase {
            name: "z10_la_wide",
            bounds: Bounds::new(33.70, -118.55, 34.20, -117.80),
            zoom: 10,
        });
    }
    cases.extend([
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
    ]);

    println!(
        "stress config: iterations={} sleep_seconds={} max_drop_pp={} include_z10={}",
        iterations, sleep_seconds, max_drop_pp, include_z10
    );

    let client = TrafficClient::new()?;
    let mut failures = Vec::new();

    for case in cases {
        println!("\n=== {} (z{}) ===", case.name, case.zoom);

        let load_start = Instant::now();
        let network =
            OsmRoadNetwork::from_pbf_for_traffic_bounds(&pbf_path, &case.bounds, case.zoom)?;
        let load_ms = load_start.elapsed().as_millis();
        let cfg = MatchConfig::for_zoom(case.zoom);
        println!(
            "network: edges={} load_ms={} cfg=(max_distance_m={}, max_bearing_diff_deg={})",
            network.edge_count(),
            load_ms,
            cfg.max_distance_m,
            cfg.max_bearing_diff_deg,
        );

        let mut baseline_rate: Option<f64> = None;
        let mut min_rate = f64::INFINITY;
        let mut max_rate = f64::NEG_INFINITY;
        let mut worst_drop_pp = 0.0f64;

        for i in 0..iterations {
            let fetch_start = Instant::now();
            let tiles = client.get_traffic(&case.bounds, case.zoom).await?;
            let fetch_ms = fetch_start.elapsed().as_millis();

            let total_segments: usize = tiles
                .iter()
                .flat_map(|t| &t.features)
                .map(|f| f.segments.len())
                .sum();

            let match_start = Instant::now();
            let out = network.match_tiles(&tiles, &cfg);
            let match_ms = match_start.elapsed().as_millis();

            let matched = total_segments.saturating_sub(out.unmatched.len());
            let rate = (matched as f64) * 100.0 / (total_segments.max(1) as f64);
            let baseline = *baseline_rate.get_or_insert(rate);
            let delta_pp = rate - baseline;
            let drop_pp = (baseline - rate).max(0.0);

            min_rate = min_rate.min(rate);
            max_rate = max_rate.max(rate);
            worst_drop_pp = worst_drop_pp.max(drop_pp);

            println!(
                "iter={:02} ts={} tiles={} segments={} matched={} unmatched={} rate={:.2}% delta_pp={:+.2} fetch_ms={} match_ms={}",
                i + 1,
                utc_timestamp_secs(),
                tiles.len(),
                total_segments,
                matched,
                out.unmatched.len(),
                rate,
                delta_pp,
                fetch_ms,
                match_ms,
            );

            if i + 1 < iterations && sleep_seconds > 0 {
                tokio::time::sleep(Duration::from_secs(sleep_seconds as u64)).await;
            }
        }

        let baseline = baseline_rate.unwrap_or(0.0);
        let span_pp = max_rate - min_rate;
        println!(
            "summary: baseline={:.2}% min={:.2}% max={:.2}% span_pp={:.2} worst_drop_pp={:.2}",
            baseline, min_rate, max_rate, span_pp, worst_drop_pp
        );

        if worst_drop_pp > max_drop_pp {
            failures.push(format!(
                "{} worst_drop_pp {:.2} exceeded threshold {:.2}",
                case.name, worst_drop_pp, max_drop_pp
            ));
        }
    }

    if !failures.is_empty() {
        eprintln!("\nFAIL: stress regression threshold exceeded");
        for f in failures {
            eprintln!("  - {f}");
        }
        std::process::exit(2);
    }

    Ok(())
}
