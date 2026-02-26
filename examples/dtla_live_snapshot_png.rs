use google_maps_traffic::{
    Bounds, CongestionLevel, DayOfWeek, LatLng, TileCoord, TrafficClient, TrafficFeature,
};
use image::{Rgba, RgbaImage, imageops};
use imageproc::drawing::draw_antialiased_line_segment_mut;
use imageproc::pixelops::interpolate;
use std::error::Error;
use std::path::Path;

const TILE_SIZE: u32 = 256;
const ZOOM: u8 = 15;
const DTLA_BOUNDS: Bounds = Bounds {
    south: 33.999,
    west: -118.265,
    north: 34.065,
    east: -118.225,
};

const COLOR_NORMAL: Rgba<u8> = Rgba([0, 190, 0, 255]);
const COLOR_SLOW: Rgba<u8> = Rgba([255, 210, 0, 255]);
const COLOR_HEAVY: Rgba<u8> = Rgba([255, 140, 0, 255]);
const COLOR_SEVERE: Rgba<u8> = Rgba([220, 20, 30, 255]);
const COLOR_UNKNOWN: Rgba<u8> = Rgba([160, 160, 160, 255]);

struct Basemap {
    img: RgbaImage,
    min_x: u32,
    min_y: u32,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let output_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "artifacts/visual_smoke/dtla_live_snapshot.png".to_string());
    if let Some(parent) = Path::new(&output_path).parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent)?;
    }

    let client = TrafficClient::new()?;
    let typical_hour = std::env::var("TYPICAL_HOUR")
        .ok()
        .and_then(|s| s.parse::<u8>().ok())
        .filter(|h| *h <= 23);

    let tiles = if let Some(hour) = typical_hour {
        let t = client
            .get_typical_traffic(&DTLA_BOUNDS, ZOOM, DayOfWeek::Monday, hour)
            .await?;
        println!(
            "typical snapshot fetched: day=Monday hour={hour:02}:00 tiles={} features={}",
            t.len(),
            t.iter().map(|tile| tile.features.len()).sum::<usize>()
        );
        t
    } else {
        let t = client.get_traffic(&DTLA_BOUNDS, ZOOM).await?;
        println!(
            "live snapshot fetched: tiles={} features={}",
            t.len(),
            t.iter().map(|tile| tile.features.len()).sum::<usize>()
        );
        t
    };

    let basemap = fetch_osm_basemap(DTLA_BOUNDS, ZOOM).await?;
    let mut img = basemap.img.clone();
    for tile in &tiles {
        for feature in &tile.features {
            draw_feature(&mut img, &basemap, tile.coord.zoom, feature);
        }
    }

    img.save(&output_path)?;
    println!("wrote {}", output_path);
    Ok(())
}

async fn fetch_osm_basemap(bounds: Bounds, zoom: u8) -> Result<Basemap, Box<dyn Error>> {
    let tiles = TileCoord::tiles_for_bounds(&bounds, zoom)?;
    if tiles.is_empty() {
        return Err("no tiles for provided bounds".into());
    }

    let mut min_x = u32::MAX;
    let mut max_x = 0u32;
    let mut min_y = u32::MAX;
    let mut max_y = 0u32;
    for t in &tiles {
        min_x = min_x.min(t.x);
        max_x = max_x.max(t.x);
        min_y = min_y.min(t.y);
        max_y = max_y.max(t.y);
    }

    let width = (max_x - min_x + 1) * TILE_SIZE;
    let height = (max_y - min_y + 1) * TILE_SIZE;
    let mut canvas = RgbaImage::from_pixel(width, height, Rgba([245, 245, 245, 255]));

    let http = reqwest::Client::builder()
        .user_agent("google_maps_traffic/0.1 (dtla-live-snapshot)")
        .build()?;

    for y in min_y..=max_y {
        for x in min_x..=max_x {
            let tile = fetch_osm_tile_png(&http, zoom, x, y).await?;
            imageops::overlay(
                &mut canvas,
                &tile,
                ((x - min_x) * TILE_SIZE) as i64,
                ((y - min_y) * TILE_SIZE) as i64,
            );
        }
    }

    Ok(Basemap {
        img: canvas,
        min_x,
        min_y,
    })
}

async fn fetch_osm_tile_png(
    http: &reqwest::Client,
    zoom: u8,
    x: u32,
    y: u32,
) -> Result<RgbaImage, Box<dyn Error>> {
    let hosts = [
        "https://tile.openstreetmap.org",
        "https://tile.openstreetmap.de",
        "https://a.tile.openstreetmap.fr/hot",
    ];

    for host in hosts {
        let url = format!("{host}/{zoom}/{x}/{y}.png");
        if let Ok(response) = http.get(url).send().await
            && response.status().is_success()
        {
            let bytes = response.bytes().await?;
            return Ok(image::load_from_memory(&bytes)?.to_rgba8());
        }
    }

    Ok(RgbaImage::from_pixel(
        TILE_SIZE,
        TILE_SIZE,
        Rgba([238, 238, 238, 255]),
    ))
}

fn draw_feature(img: &mut RgbaImage, base: &Basemap, zoom: u8, feature: &TrafficFeature) {
    if feature.geometry.len() < 2 {
        return;
    }

    let points: Vec<(f64, f64)> = feature
        .geometry
        .iter()
        .map(|p| latlng_to_pixel(p, zoom, base.min_x, base.min_y))
        .collect();

    for seg in &feature.segments {
        let color = congestion_color(seg.congestion);
        let start = seg.start_index.min(points.len().saturating_sub(1));
        let end = seg.end_index.min(points.len().saturating_sub(1));
        if end <= start {
            continue;
        }

        for i in start..end {
            let (x1, y1) = points[i];
            let (x2, y2) = points[i + 1];
            draw_thick_line(
                img,
                x1.round() as i32,
                y1.round() as i32,
                x2.round() as i32,
                y2.round() as i32,
                color,
                3,
            );
        }
    }
}

fn draw_thick_line(
    img: &mut RgbaImage,
    x1: i32,
    y1: i32,
    x2: i32,
    y2: i32,
    color: Rgba<u8>,
    radius: i32,
) {
    for dx in -radius..=radius {
        for dy in -radius..=radius {
            if dx * dx + dy * dy > radius * radius {
                continue;
            }
            draw_antialiased_line_segment_mut(
                img,
                (x1 + dx, y1 + dy),
                (x2 + dx, y2 + dy),
                color,
                interpolate,
            );
        }
    }
}

fn congestion_color(c: CongestionLevel) -> Rgba<u8> {
    match c {
        CongestionLevel::Normal => COLOR_NORMAL,
        CongestionLevel::Slow => COLOR_SLOW,
        CongestionLevel::Heavy => COLOR_HEAVY,
        CongestionLevel::Severe => COLOR_SEVERE,
        CongestionLevel::Unknown(_) => COLOR_UNKNOWN,
    }
}

fn latlng_to_pixel(p: &LatLng, zoom: u8, min_x: u32, min_y: u32) -> (f64, f64) {
    let world = 256.0 * (1u32 << zoom) as f64;
    let x_world = (p.lng + 180.0) / 360.0 * world;
    let lat_rad = p.lat.to_radians();
    let y_world = (1.0 - lat_rad.tan().asinh() / std::f64::consts::PI) / 2.0 * world;
    (
        x_world - (min_x as f64 * TILE_SIZE as f64),
        y_world - (min_y as f64 * TILE_SIZE as f64),
    )
}
