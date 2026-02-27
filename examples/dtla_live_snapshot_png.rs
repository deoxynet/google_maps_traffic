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
const FLOW_LATERAL_OFFSET_PX: f64 = 1.2;
const FLOW_ARROW_LENGTH_PX: f64 = 10.0;
const FLOW_ARROW_HALF_WIDTH_PX: f64 = 4.0;
const FLOW_ARROW_OUTLINE: Rgba<u8> = Rgba([0, 0, 0, 255]);

struct Basemap {
    img: RgbaImage,
    min_x: u32,
    min_y: u32,
}

#[derive(Clone, Copy)]
struct ShiftedSegment {
    x1: f64,
    y1: f64,
    x2: f64,
    y2: f64,
    ux: f64,
    uy: f64,
    len: f64,
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
            draw_directed_stroke(img, x1, y1, x2, y2, color);
        }
        draw_segment_flow_arrow(img, &points, start, end, color);
    }
}

fn draw_directed_stroke(img: &mut RgbaImage, x1: f64, y1: f64, x2: f64, y2: f64, color: Rgba<u8>) {
    if let Some(s) = shift_segment_for_flow(x1, y1, x2, y2) {
        draw_thick_line(
            img,
            s.x1.round() as i32,
            s.y1.round() as i32,
            s.x2.round() as i32,
            s.y2.round() as i32,
            color,
            3,
        );
    }
}

fn draw_segment_flow_arrow(
    img: &mut RgbaImage,
    points: &[(f64, f64)],
    start: usize,
    end: usize,
    color: Rgba<u8>,
) {
    if end <= start || end > points.len().saturating_sub(1) {
        return;
    }

    let mid = start + (end - start) / 2;
    for delta in 0..=(end - start) {
        let upper = mid + delta;
        if upper < end && draw_arrow_for_edge(img, points[upper], points[upper + 1], color) {
            return;
        }

        if delta > 0 {
            let lower = mid.saturating_sub(delta);
            if lower >= start
                && lower < end
                && draw_arrow_for_edge(img, points[lower], points[lower + 1], color)
            {
                return;
            }
        }
    }
}

fn draw_arrow_for_edge(
    img: &mut RgbaImage,
    p1: (f64, f64),
    p2: (f64, f64),
    color: Rgba<u8>,
) -> bool {
    let Some(s) = shift_segment_for_flow(p1.0, p1.1, p2.0, p2.1) else {
        return false;
    };
    if s.len < FLOW_ARROW_LENGTH_PX * 1.2 {
        return false;
    }

    let tip_t = 0.75;
    let tip_x = s.x1 + (s.x2 - s.x1) * tip_t;
    let tip_y = s.y1 + (s.y2 - s.y1) * tip_t;
    let base_x = tip_x - s.ux * FLOW_ARROW_LENGTH_PX;
    let base_y = tip_y - s.uy * FLOW_ARROW_LENGTH_PX;
    let nx = -s.uy;
    let ny = s.ux;

    let left_x = base_x + nx * FLOW_ARROW_HALF_WIDTH_PX;
    let left_y = base_y + ny * FLOW_ARROW_HALF_WIDTH_PX;
    let right_x = base_x - nx * FLOW_ARROW_HALF_WIDTH_PX;
    let right_y = base_y - ny * FLOW_ARROW_HALF_WIDTH_PX;

    draw_thick_line(
        img,
        tip_x.round() as i32,
        tip_y.round() as i32,
        left_x.round() as i32,
        left_y.round() as i32,
        FLOW_ARROW_OUTLINE,
        2,
    );
    draw_thick_line(
        img,
        tip_x.round() as i32,
        tip_y.round() as i32,
        right_x.round() as i32,
        right_y.round() as i32,
        FLOW_ARROW_OUTLINE,
        2,
    );
    draw_thick_line(
        img,
        tip_x.round() as i32,
        tip_y.round() as i32,
        base_x.round() as i32,
        base_y.round() as i32,
        FLOW_ARROW_OUTLINE,
        2,
    );
    draw_thick_line(
        img,
        tip_x.round() as i32,
        tip_y.round() as i32,
        left_x.round() as i32,
        left_y.round() as i32,
        color,
        1,
    );
    draw_thick_line(
        img,
        tip_x.round() as i32,
        tip_y.round() as i32,
        right_x.round() as i32,
        right_y.round() as i32,
        color,
        1,
    );
    draw_thick_line(
        img,
        tip_x.round() as i32,
        tip_y.round() as i32,
        base_x.round() as i32,
        base_y.round() as i32,
        color,
        1,
    );
    true
}

fn shift_segment_for_flow(x1: f64, y1: f64, x2: f64, y2: f64) -> Option<ShiftedSegment> {
    let dx = x2 - x1;
    let dy = y2 - y1;
    let len = (dx * dx + dy * dy).sqrt();
    if !len.is_finite() || len <= 0.5 {
        return None;
    }

    let ux = dx / len;
    let uy = dy / len;
    let nx = -uy;
    let ny = ux;
    let ox = nx * FLOW_LATERAL_OFFSET_PX;
    let oy = ny * FLOW_LATERAL_OFFSET_PX;

    Some(ShiftedSegment {
        x1: x1 + ox,
        y1: y1 + oy,
        x2: x2 + ox,
        y2: y2 + oy,
        ux,
        uy,
        len,
    })
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
