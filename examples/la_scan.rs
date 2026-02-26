use google_maps_traffic::{Bounds, CongestionLevel, TileCoord, TrafficClient};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = TrafficClient::new()?;

    // Downtown LA bounding box
    // Roughly: Union Station (NE) to Convention Center (SW)
    let bounds = Bounds::new(33.999, -118.265, 34.065, -118.225);
    let zoom = 14;

    let tiles_needed = TileCoord::tiles_for_bounds(&bounds, zoom)?;
    println!("Downtown LA scan @ zoom {zoom}");
    println!("Bounds: {bounds:?}");
    println!("Tiles to fetch: {}\n", tiles_needed.len());

    let tiles = client.get_traffic(&bounds, zoom).await?;

    let total_features: usize = tiles.iter().map(|t| t.features.len()).sum();
    let total_incidents: usize = tiles.iter().map(|t| t.incidents.len()).sum();

    println!("Results:");
    println!("  Tiles returned: {}", tiles.len());
    println!("  Total features: {total_features}");
    println!("  Total incidents: {total_incidents}");

    // Congestion breakdown
    let mut normal = 0usize;
    let mut slow = 0usize;
    let mut heavy = 0usize;
    let mut severe = 0usize;
    let mut unknown = 0usize;
    let mut total_segments = 0usize;
    let mut total_points = 0usize;

    // Layer type breakdown
    let mut layer_counts: std::collections::HashMap<u32, usize> = std::collections::HashMap::new();

    for tile in &tiles {
        for feature in &tile.features {
            total_points += feature.geometry.len();
            *layer_counts.entry(feature.layer_type).or_default() += 1;
            for seg in &feature.segments {
                total_segments += 1;
                match seg.congestion {
                    CongestionLevel::Normal => normal += 1,
                    CongestionLevel::Slow => slow += 1,
                    CongestionLevel::Heavy => heavy += 1,
                    CongestionLevel::Severe => severe += 1,
                    CongestionLevel::Unknown(_) => unknown += 1,
                }
            }
        }
    }

    println!("  Total geometry points: {total_points}");
    println!("  Total segments: {total_segments}");
    println!("\nCongestion breakdown:");
    println!("  Normal: {normal}");
    println!("  Slow:   {slow}");
    println!("  Heavy:  {heavy}");
    println!("  Severe: {severe}");
    if unknown > 0 {
        println!("  Unknown: {unknown}");
    }

    println!("\nLayer types:");
    let mut layers: Vec<_> = layer_counts.into_iter().collect();
    layers.sort_by_key(|(k, _)| *k);
    for (layer, count) in &layers {
        println!("  {layer}: {count} features");
    }

    // Sample some geometry to check coordinates are in LA
    println!("\nSample coordinates (should be in downtown LA ~34.0N, ~118.2W):");
    let mut shown = 0;
    for tile in &tiles {
        for feature in &tile.features {
            if feature.geometry.len() > 5 && shown < 5 {
                let first = &feature.geometry[0];
                let last = &feature.geometry[feature.geometry.len() - 1];
                let congestion = feature
                    .segments
                    .first()
                    .map(|s| format!("{:?}", s.congestion))
                    .unwrap_or_else(|| "none".into());
                println!(
                    "  layer={:3} pts={:3} ({:.4}, {:.4}) -> ({:.4}, {:.4}) [{}]",
                    feature.layer_type,
                    feature.geometry.len(),
                    first.lat,
                    first.lng,
                    last.lat,
                    last.lng,
                    congestion,
                );
                shown += 1;
            }
        }
    }

    // Show incidents
    if total_incidents > 0 {
        println!("\nIncidents:");
        for tile in &tiles {
            for incident in &tile.incidents {
                println!(
                    "  [{:?}] {}",
                    incident.incident_type,
                    incident
                        .description
                        .as_deref()
                        .or(incident.short_description.as_deref())
                        .unwrap_or("(no description)")
                );
            }
        }
    }

    Ok(())
}
