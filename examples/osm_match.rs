use google_maps_traffic::{
    Bounds, CongestionLevel, EdgeTravelDirection, MatchConfig, OsmRoadNetwork,
    SpeedEstimationConfig, TrafficClient,
};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let pbf_path = std::env::args().nth(1).ok_or_else(|| {
        "usage: cargo run --example osm_match --features osm -- <path/to.osm.pbf>".to_string()
    })?;

    let bounds = Bounds::new(33.999, -118.265, 34.065, -118.225);
    let zoom = 14;
    let config = MatchConfig::for_zoom(zoom);
    let speed_config = SpeedEstimationConfig::default();
    let hour_utc = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .ok()
        .map(|d| ((d.as_secs() / 3600) % 24) as u8);

    println!("Loading OSM network from: {pbf_path}");
    let network = OsmRoadNetwork::from_pbf_for_traffic_bounds(&pbf_path, &bounds, zoom)?;
    println!(
        "Loaded {} OSM edges for tile-expanded traffic bounds",
        network.edge_count()
    );
    println!(
        "Match config: max_distance_m={} max_bearing_diff_deg={} weights=({}, {})",
        config.max_distance_m,
        config.max_bearing_diff_deg,
        config.distance_weight,
        config.bearing_weight,
    );
    if let Some(hour) = hour_utc {
        println!("Speed model hour (UTC): {hour}");
    }

    println!("Fetching live traffic tiles...");
    let client = TrafficClient::new()?;
    let tiles = client.get_traffic(&bounds, zoom).await?;
    let total_segments: usize = tiles
        .iter()
        .flat_map(|tile| &tile.features)
        .map(|feature| feature.segments.len())
        .sum();
    println!(
        "Fetched {} tiles containing {} traffic segments",
        tiles.len(),
        total_segments
    );

    let output = network.match_tiles(&tiles, &config);
    let unmatched_segments = output.unmatched.len();
    let matched_segments = total_segments.saturating_sub(unmatched_segments);
    let match_rate = if total_segments == 0 {
        0.0
    } else {
        (matched_segments as f64) / (total_segments as f64) * 100.0
    };

    println!("\nMatch results:");
    println!("  Matched segments: {matched_segments}");
    println!("  Unmatched segments: {unmatched_segments}");
    println!("  Match rate: {match_rate:.1}%");
    println!("  Unique matched OSM edges: {}", output.matches.len());

    let forward_matches = output
        .matches
        .iter()
        .filter(|m| matches!(m.direction, EdgeTravelDirection::Forward))
        .count();
    let reverse_matches = output.matches.len().saturating_sub(forward_matches);
    println!("  Directional split: forward={forward_matches} reverse={reverse_matches}");

    let mut normal = 0usize;
    let mut slow = 0usize;
    let mut heavy = 0usize;
    let mut severe = 0usize;
    let mut unknown = 0usize;

    for m in &output.matches {
        match m.congestion {
            CongestionLevel::Normal => normal += 1,
            CongestionLevel::Slow => slow += 1,
            CongestionLevel::Heavy => heavy += 1,
            CongestionLevel::Severe => severe += 1,
            CongestionLevel::Unknown(_) => unknown += 1,
        }
    }

    println!("\nCongestion by matched edge:");
    println!("  Normal: {normal}");
    println!("  Slow:   {slow}");
    println!("  Heavy:  {heavy}");
    println!("  Severe: {severe}");
    if unknown > 0 {
        println!("  Unknown: {unknown}");
    }

    let mut severe_matches: Vec<_> = output
        .matches
        .iter()
        .filter(|m| matches!(m.congestion, CongestionLevel::Severe))
        .collect();
    severe_matches.sort_by(|a, b| {
        a.score
            .partial_cmp(&b.score)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    if !severe_matches.is_empty() {
        println!("\nTop severe edge matches:");
        for m in severe_matches.into_iter().take(10) {
            let est_speed = m
                .estimate_speed_in_match_direction_kph(hour_utc, &speed_config)
                .unwrap_or_else(|| m.estimate_speed_kph(hour_utc, &speed_config));
            println!(
                "  way={} {}->{} {} dir={:?} limit={:.1}kph est={:.1}kph dist={:.1}m bearing_diff={:.1} score={:.3}",
                m.edge.way_id,
                m.edge.start_node_id,
                m.edge.end_node_id,
                m.edge.highway,
                m.direction,
                m.edge.max_speed_kph,
                est_speed,
                m.distance_m,
                m.bearing_diff_deg,
                m.score,
            );
        }
    }

    Ok(())
}
