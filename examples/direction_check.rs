use google_maps_traffic::{Bounds, TrafficClient};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = TrafficClient::new()?;

    // Small area around the 101/110 interchange
    let bounds = Bounds::new(34.055, -118.250, 34.070, -118.235);
    let zoom = 14;

    let tiles = client.get_traffic(&bounds, zoom).await?;
    let all_features: Vec<_> = tiles.iter().flat_map(|t| &t.features).collect();
    println!("Features: {}\n", all_features.len());

    for (i, f) in all_features.iter().enumerate() {
        if f.geometry.len() < 3 {
            continue;
        }
        let start = &f.geometry[0];
        let end = &f.geometry[f.geometry.len() - 1];
        let cong = f
            .segments
            .first()
            .map(|s| format!("{:?}", s.congestion))
            .unwrap_or_else(|| "?".into());

        println!(
            "  [{i:2}] layer={:3} {:>2} {:5.1}° pts={:3} ({:.4},{:.4})->({:.4},{:.4}) [{cong}]",
            f.layer_type,
            f.compass(),
            f.bearing,
            f.geometry.len(),
            start.lat,
            start.lng,
            end.lat,
            end.lng,
        );
    }

    Ok(())
}
