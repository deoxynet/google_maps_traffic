use google_maps_traffic::{Bounds, CongestionLevel, DayOfWeek, TrafficClient};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let client = TrafficClient::new()?;

    // Downtown LA bounding box
    let bounds = Bounds::new(33.999, -118.265, 34.065, -118.225);
    let zoom = 14;

    println!("=== LIVE TRAFFIC ===");
    let live_tiles = client.get_traffic(&bounds, zoom).await?;
    print_summary("Live", &live_tiles);

    println!("\n=== TYPICAL: Monday 8 AM ===");
    let mon_8am = client
        .get_typical_traffic(&bounds, zoom, DayOfWeek::Monday, 8)
        .await?;
    print_summary("Mon 8AM", &mon_8am);

    println!("\n=== TYPICAL: Friday 5 PM ===");
    let fri_5pm = client
        .get_typical_traffic(&bounds, zoom, DayOfWeek::Friday, 17)
        .await?;
    print_summary("Fri 5PM", &fri_5pm);

    println!("\n=== TYPICAL: Sunday 3 AM ===");
    let sun_3am = client
        .get_typical_traffic(&bounds, zoom, DayOfWeek::Sunday, 3)
        .await?;
    print_summary("Sun 3AM", &sun_3am);

    Ok(())
}

fn print_summary(label: &str, tiles: &[google_maps_traffic::TrafficTile]) {
    let total_features: usize = tiles.iter().map(|t| t.features.len()).sum();
    let total_incidents: usize = tiles.iter().map(|t| t.incidents.len()).sum();
    let total_points: usize = tiles
        .iter()
        .flat_map(|t| &t.features)
        .map(|f| f.geometry.len())
        .sum();

    let mut normal = 0usize;
    let mut slow = 0usize;
    let mut heavy = 0usize;
    let mut severe = 0usize;
    let mut unknown = 0usize;

    for tile in tiles {
        for feature in &tile.features {
            for seg in &feature.segments {
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

    println!("{label}:");
    println!("  Tiles: {}", tiles.len());
    println!("  Features: {total_features}");
    println!("  Geometry points: {total_points}");
    println!("  Incidents: {total_incidents}");
    println!(
        "  Congestion: normal={normal} slow={slow} heavy={heavy} severe={severe} unknown={unknown}"
    );
}
