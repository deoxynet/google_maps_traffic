use google_maps_traffic::{
    CongestionLevel, DayOfWeek, TileCoord, TrafficClient, TrafficTile,
};
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::time::{Duration, Instant};

/// Poll interval between live tile fetches.
const POLL_INTERVAL: Duration = Duration::from_secs(10);
/// Total number of polls (40 × 10s ≈ 7 minutes).
const POLL_COUNT: usize = 40;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    // DTLA tile at zoom 15 — covers roughly 7th & Figueroa area.
    let coord = TileCoord::new(15, 5621, 13085)?;
    let client = TrafficClient::new()?;

    // === 1. Typical traffic cache TTLs ===
    println!("=== Typical Traffic Cache TTLs ===");
    match client
        .get_typical_ttls(&[coord], DayOfWeek::Friday, 9)
        .await
    {
        Ok(ttls) => {
            for (i, ttl) in ttls.iter().enumerate() {
                println!("  tile {i}: cache TTL = {ttl:.1}s ({:.1}min)", ttl / 60.0);
            }
        }
        Err(e) => println!("  (failed: {e})"),
    }

    // === 2. Poll live traffic and detect changes ===
    println!(
        "\n=== Polling live traffic every {}s ({} polls, ~{}min) ===",
        POLL_INTERVAL.as_secs(),
        POLL_COUNT,
        POLL_INTERVAL.as_secs() * POLL_COUNT as u64 / 60,
    );
    println!(
        "{:>6} {:>4} {:>18} {:>5} {:>4} {:>30} {:>6}  {}",
        "time", "poll", "hash", "feats", "incs", "congestion", "ms", "notes"
    );
    println!("{}", "-".repeat(100));

    let mut prev_hash: Option<u64> = None;
    let mut change_times: Vec<Instant> = Vec::new();
    let mut all_headers_printed = false;
    let start = Instant::now();

    for poll in 0..POLL_COUNT {
        let t = Instant::now();
        let (tile, headers) = client.get_tile_with_headers(coord).await?;
        let fetch_ms = t.elapsed().as_millis();

        // Print full headers on first poll only.
        if !all_headers_printed {
            println!("\n  HTTP response headers:");
            for (k, v) in &headers {
                println!("    {k}: {v}");
            }
            println!();
            all_headers_printed = true;
        }

        let hash = hash_tile_state(&tile);
        let changed = prev_hash.map_or(false, |prev| prev != hash);

        let elapsed = start.elapsed().as_secs();
        let congestion = summarize_congestion(&tile);

        // Extract interesting headers for compact display.
        let cache_ctl = headers
            .iter()
            .find(|(k, _)| k == "cache-control")
            .map(|(_, v)| v.as_str())
            .unwrap_or("-");

        let mut notes = String::new();
        if poll == 0 {
            notes.push_str(&format!("cc={cache_ctl}"));
        }
        if changed {
            notes.push_str(" << CHANGED");
        }

        println!(
            "{:>5}s {:>4} {:>18} {:>5} {:>4} {:>30} {:>5}ms  {}",
            elapsed,
            poll,
            format!("{:016x}", hash),
            tile.features.len(),
            tile.incidents.len(),
            congestion,
            fetch_ms,
            notes.trim(),
        );

        if changed {
            change_times.push(Instant::now());
        }
        if poll == 0 && prev_hash.is_none() {
            // Record initial fetch as first "change" for interval calc
            change_times.push(Instant::now());
        }
        prev_hash = Some(hash);

        if poll < POLL_COUNT - 1 {
            tokio::time::sleep(POLL_INTERVAL).await;
        }
    }

    // === Summary ===
    println!("\n=== Summary ===");
    println!("Total polls: {POLL_COUNT}");
    println!(
        "Data changes detected: {} (excluding initial fetch)",
        change_times.len().saturating_sub(1)
    );
    if change_times.len() > 1 {
        let intervals: Vec<f64> = change_times
            .windows(2)
            .map(|w| w[1].duration_since(w[0]).as_secs_f64())
            .collect();
        let avg = intervals.iter().sum::<f64>() / intervals.len() as f64;
        let min = intervals.iter().cloned().fold(f64::INFINITY, f64::min);
        let max = intervals.iter().cloned().fold(0.0f64, f64::max);
        println!("Change intervals: min={min:.0}s, avg={avg:.0}s, max={max:.0}s");
        for (i, interval) in intervals.iter().enumerate() {
            println!("  change {}: {interval:.0}s after previous", i + 1);
        }
    } else {
        println!("No changes detected in {} polls — data was static the entire time.",
            POLL_COUNT);
    }

    Ok(())
}

fn hash_tile_state(tile: &TrafficTile) -> u64 {
    let mut hasher = DefaultHasher::new();
    tile.features.len().hash(&mut hasher);
    for feature in &tile.features {
        feature.geometry.len().hash(&mut hasher);
        feature.layer_type.hash(&mut hasher);
        for seg in &feature.segments {
            seg.start_index.hash(&mut hasher);
            seg.end_index.hash(&mut hasher);
            congestion_byte(&seg.congestion).hash(&mut hasher);
        }
    }
    tile.incidents.len().hash(&mut hasher);
    hasher.finish()
}

fn congestion_byte(c: &CongestionLevel) -> u8 {
    match c {
        CongestionLevel::Normal => 0,
        CongestionLevel::Slow => 1,
        CongestionLevel::Heavy => 2,
        CongestionLevel::Severe => 3,
        CongestionLevel::Unknown(v) => *v,
    }
}

fn summarize_congestion(tile: &TrafficTile) -> String {
    let (mut n, mut s, mut h, mut sv) = (0usize, 0usize, 0usize, 0usize);
    for f in &tile.features {
        for seg in &f.segments {
            match seg.congestion {
                CongestionLevel::Normal => n += 1,
                CongestionLevel::Slow => s += 1,
                CongestionLevel::Heavy => h += 1,
                CongestionLevel::Severe => sv += 1,
                _ => {}
            }
        }
    }
    format!("N:{n} S:{s} H:{h} SV:{sv}")
}
