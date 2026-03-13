//! Export 24 hours of typical traffic lane data as JSON for the browser particle renderer.
//!
//! Fetches typical Monday traffic for every hour and projects all road
//! segments to pixel coordinates, writing a compact JSON file consumed
//! by `web/index.html`.
//!
//! Usage:
//!   cargo run --release --example export_lanes
//!
//! Environment:
//!   OUTPUT=web/lanes.json   Output path (default)
//!   LIVE=1                  Export a single live snapshot instead of 24h typical

use google_maps_traffic::{
    Bounds, CongestionLevel, DayOfWeek, LatLng, TileCoord, TrafficClient, TrafficTile,
};
use std::error::Error;
use std::path::Path;

const TILE_SIZE: u32 = 256;
const ZOOM: u8 = 15;
const LANE_OFFSET_PX: f64 = 1.5;

const BOUNDS: Bounds = Bounds {
    south: 34.015,
    west: -118.260,
    north: 34.060,
    east: -118.230,
};

struct Canvas {
    w: u32,
    h: u32,
    min_tx: u32,
    min_ty: u32,
}

impl Canvas {
    fn from_bounds(bounds: &Bounds, zoom: u8) -> Result<Self, Box<dyn Error>> {
        let tiles = TileCoord::tiles_for_bounds(bounds, zoom)?;
        let (mut x0, mut x1, mut y0, mut y1) = (u32::MAX, 0u32, u32::MAX, 0u32);
        for t in &tiles {
            x0 = x0.min(t.x);
            x1 = x1.max(t.x);
            y0 = y0.min(t.y);
            y1 = y1.max(t.y);
        }
        Ok(Self {
            w: (x1 - x0 + 1) * TILE_SIZE,
            h: (y1 - y0 + 1) * TILE_SIZE,
            min_tx: x0,
            min_ty: y0,
        })
    }

    fn project(&self, p: &LatLng) -> (f64, f64) {
        let world = 256.0 * (1u64 << ZOOM) as f64;
        let x = (p.lng + 180.0) / 360.0 * world - self.min_tx as f64 * TILE_SIZE as f64;
        let lat_r = p.lat.to_radians();
        let y = (1.0 - lat_r.tan().asinh() / std::f64::consts::PI) / 2.0 * world
            - self.min_ty as f64 * TILE_SIZE as f64;
        (x, y)
    }
}

fn congestion_u8(c: CongestionLevel) -> u8 {
    match c {
        CongestionLevel::Normal => 0,
        CongestionLevel::Slow => 1,
        CongestionLevel::Heavy => 2,
        CongestionLevel::Severe => 3,
        CongestionLevel::Unknown(_) => 4,
    }
}

fn offset_polyline(pts: &[(f64, f64)], offset: f64) -> Vec<(f64, f64)> {
    let n = pts.len();
    if n < 2 {
        return pts.to_vec();
    }
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let (prev, next) = if i == 0 {
            (pts[0], pts[1])
        } else if i == n - 1 {
            (pts[n - 2], pts[n - 1])
        } else {
            (pts[i - 1], pts[i + 1])
        };
        let dx = next.0 - prev.0;
        let dy = next.1 - prev.1;
        let len = (dx * dx + dy * dy).sqrt();
        if len < 0.01 {
            out.push(pts[i]);
        } else {
            let nx = dy / len;
            let ny = -dx / len;
            out.push((pts[i].0 + nx * offset, pts[i].1 + ny * offset));
        }
    }
    out
}

fn tiles_to_lane_json(tiles: &[TrafficTile], cv: &Canvas) -> (String, usize) {
    let mut json = String::with_capacity(128 * 1024);
    json.push('[');
    let mut count = 0usize;

    for tile in tiles {
        for feat in &tile.features {
            if feat.geometry.len() < 2 {
                continue;
            }
            let raw: Vec<(f64, f64)> = feat.geometry.iter().map(|p| cv.project(p)).collect();

            for seg in &feat.segments {
                let s = seg.start_index.min(raw.len() - 1);
                let e = seg.end_index.min(raw.len() - 1);
                if e <= s {
                    continue;
                }
                let pts_raw = &raw[s..=e];
                if pts_raw.len() < 2 {
                    continue;
                }

                let pts = offset_polyline(pts_raw, LANE_OFFSET_PX);

                let mut total_len = 0.0f64;
                for i in 1..pts.len() {
                    let dx = pts[i].0 - pts[i - 1].0;
                    let dy = pts[i].1 - pts[i - 1].1;
                    total_len += (dx * dx + dy * dy).sqrt();
                }
                if total_len < 3.0 {
                    continue;
                }

                if count > 0 {
                    json.push(',');
                }
                json.push_str(r#"{"p":["#);
                for (j, &(x, y)) in pts.iter().enumerate() {
                    if j > 0 {
                        json.push(',');
                    }
                    json.push_str(&format!("[{:.1},{:.1}]", x, y));
                }
                json.push_str(&format!(r#"],"c":{}}}"#, congestion_u8(seg.congestion)));
                count += 1;
            }
        }
    }

    json.push(']');
    (json, count)
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let output = std::env::var("OUTPUT").unwrap_or_else(|_| "web/lanes.json".into());
    if let Some(p) = Path::new(&output).parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(p)?;
    }

    let client = TrafficClient::new()?;
    let cv = Canvas::from_bounds(&BOUNDS, ZOOM)?;
    println!("canvas {}x{}", cv.w, cv.h);

    let is_live = std::env::var("LIVE").is_ok();

    let mut json = String::with_capacity(4 * 1024 * 1024);
    json.push_str(&format!(r#"{{"w":{},"h":{},"hours":["#, cv.w, cv.h));

    if is_live {
        println!("fetching live traffic...");
        let tiles = client.get_traffic(&BOUNDS, ZOOM).await?;
        let n_feat: usize = tiles.iter().map(|t| t.features.len()).sum();
        let (lanes_json, n_lanes) = tiles_to_lane_json(&tiles, &cv);

        // Write single snapshot as hour 0, repeat for all 24
        for h in 0..24 {
            if h > 0 {
                json.push(',');
            }
            json.push_str(&lanes_json);
        }
        println!("{} features → {} lanes (live, replicated to 24h)", n_feat, n_lanes);
    } else {
        println!("fetching typical Monday traffic for 24 hours...");
        for hour in 0..24u8 {
            let tiles = client
                .get_typical_traffic(&BOUNDS, ZOOM, DayOfWeek::Monday, hour)
                .await?;
            let n_feat: usize = tiles.iter().map(|t| t.features.len()).sum();
            let (lanes_json, n_lanes) = tiles_to_lane_json(&tiles, &cv);

            if hour > 0 {
                json.push(',');
            }
            json.push_str(&lanes_json);
            println!("  {:02}:00  {} feat → {} lanes", hour, n_feat, n_lanes);
        }
    }

    json.push_str("]}");

    std::fs::write(&output, &json)?;
    let size_kb = json.len() as f64 / 1024.0;
    println!(
        "\nwrote {} ({:.0} KB)",
        output,
        size_kb,
    );
    println!("\nnext: cd web && python3 -m http.server 8080");
    println!("then open http://localhost:8080");
    Ok(())
}
