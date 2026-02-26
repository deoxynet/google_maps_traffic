use google_maps_traffic::{Bounds, CongestionLevel, TileCoord, TrafficClient};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = TrafficClient::new()?;

    // Fetch a single tile (NYC area, zoom 12)
    let coord = TileCoord::new(12, 1205, 1540)?;
    println!("Fetching tile {coord:?}...");
    let tile = client.get_tile(coord).await?;

    println!(
        "Got {} features, {} incidents",
        tile.features.len(),
        tile.incidents.len()
    );

    for (i, feature) in tile.features.iter().take(5).enumerate() {
        println!(
            "  Feature {i}: {} points, {} segments, layer={}",
            feature.geometry.len(),
            feature.segments.len(),
            feature.layer_type,
        );
        for seg in &feature.segments {
            let level = match seg.congestion {
                CongestionLevel::Normal => "normal",
                CongestionLevel::Slow => "slow",
                CongestionLevel::Heavy => "heavy",
                CongestionLevel::Severe => "severe",
                CongestionLevel::Unknown(v) => {
                    println!("    [{}-{}] unknown({v})", seg.start_index, seg.end_index);
                    continue;
                }
            };
            println!("    [{}-{}] {level}", seg.start_index, seg.end_index);
        }
    }

    for incident in &tile.incidents {
        println!(
            "  Incident: {:?} - {}",
            incident.incident_type,
            incident
                .description
                .as_deref()
                .unwrap_or("(no description)")
        );
    }

    // Fetch all tiles in a bounding box (small area of Manhattan)
    println!("\nFetching traffic for Manhattan bounding box...");
    let bounds = Bounds::new(40.748, -73.995, 40.758, -73.975);
    let tiles = client.get_traffic(&bounds, 14).await?;
    let total_features: usize = tiles.iter().map(|t| t.features.len()).sum();
    println!(
        "Got {} tiles, {} total features",
        tiles.len(),
        total_features
    );

    Ok(())
}
