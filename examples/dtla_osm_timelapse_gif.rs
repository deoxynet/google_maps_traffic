use font8x8::UnicodeFonts;
use gif::{Encoder, Frame, Repeat};
use google_maps_traffic::{
    Bounds, CongestionLevel, DayOfWeek, LatLng, TileCoord, TrafficClient, TrafficFeature,
    TrafficTile,
};
use image::{Rgba, RgbaImage, imageops};
use imageproc::drawing::{
    draw_antialiased_line_segment_mut, draw_filled_rect_mut, draw_hollow_rect_mut,
};
use imageproc::pixelops::interpolate;
use imageproc::rect::Rect;
use std::error::Error;
use std::fs::File;
use std::path::Path;

const TILE_SIZE: u32 = 256;
const ZOOM: u8 = 15;
const MINUTE_STEP: u8 = 5;
const GIF_DELAY_CS: u16 = 3;
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

const PANEL_BG: Rgba<u8> = Rgba([0, 0, 0, 170]);
const PANEL_BORDER: Rgba<u8> = Rgba([230, 230, 230, 255]);
const TEXT: Rgba<u8> = Rgba([240, 240, 240, 255]);

struct Basemap {
    img: RgbaImage,
    min_x: u32,
    min_y: u32,
}

#[derive(Default, Clone, Copy)]
struct CongestionCounts {
    normal: usize,
    slow: usize,
    heavy: usize,
    severe: usize,
    unknown: usize,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    if 60 % MINUTE_STEP != 0 {
        return Err(format!("MINUTE_STEP must divide 60, got {}", MINUTE_STEP).into());
    }

    let output_path = std::env::args()
        .nth(1)
        .unwrap_or_else(|| "artifacts/dtla_osm_typical_monday_full_day_z15_5min.gif".to_string());
    if let Some(parent) = Path::new(&output_path).parent()
        && !parent.as_os_str().is_empty()
    {
        std::fs::create_dir_all(parent)?;
    }

    let client = TrafficClient::new()?;
    let basemap = fetch_osm_basemap(DTLA_BOUNDS, ZOOM).await?;
    let mut hourly_snapshots = Vec::with_capacity(24);

    for hour in 0..24u8 {
        let tiles = client
            .get_typical_traffic(&DTLA_BOUNDS, ZOOM, DayOfWeek::Monday, hour)
            .await?;
        let frame = render_traffic_snapshot(&basemap, &tiles);
        let c = summarize_congestion(&tiles);
        hourly_snapshots.push(frame);
        println!(
            "fetched Monday {:02}:00  segments(n/s/h/v/u)={}/{}/{}/{}/{}",
            hour, c.normal, c.slow, c.heavy, c.severe, c.unknown
        );
    }

    let steps_per_hour = (60 / MINUTE_STEP) as usize;
    let total_frames = 24 * steps_per_hour;
    let width = basemap.img.width() as u16;
    let height = basemap.img.height() as u16;

    let mut file = File::create(&output_path)?;
    let mut encoder = Encoder::new(&mut file, width, height, &[])?;
    encoder.set_repeat(Repeat::Infinite)?;

    println!(
        "encoding {} frames at {}x{} (zoom={}, step={}min)",
        total_frames, width, height, ZOOM, MINUTE_STEP
    );

    for hour in 0..24usize {
        let next_hour = (hour + 1) % 24;
        for step in 0..steps_per_hour {
            let minute = (step as u8) * MINUTE_STEP;
            let alpha = minute as f32 / 60.0;
            let mut frame =
                blend_frames(&hourly_snapshots[hour], &hourly_snapshots[next_hour], alpha);
            draw_overlay_panel(&mut frame, hour as u8, minute);
            write_gif_frame(&mut encoder, frame, GIF_DELAY_CS)?;
        }
    }

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
        .user_agent("google_maps_traffic/0.1 (dtla-osm-timelapse)")
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

    let mut statuses = Vec::new();
    for host in hosts {
        let url = format!("{host}/{zoom}/{x}/{y}.png");
        match http.get(url).send().await {
            Ok(response) => {
                let status = response.status();
                if status.is_success() {
                    let bytes = response.bytes().await?;
                    return Ok(image::load_from_memory(&bytes)?.to_rgba8());
                }
                statuses.push(format!("{host}:{status}"));
            }
            Err(err) => statuses.push(format!("{host}:ERR({err})")),
        }
    }

    eprintln!(
        "warning: failed to fetch OSM tile {zoom}/{x}/{y}; fallbacks={}",
        statuses.join(", ")
    );
    Ok(RgbaImage::from_pixel(
        TILE_SIZE,
        TILE_SIZE,
        Rgba([238, 238, 238, 255]),
    ))
}

fn render_traffic_snapshot(base: &Basemap, tiles: &[TrafficTile]) -> RgbaImage {
    let mut img = base.img.clone();

    for tile in tiles {
        for feature in &tile.features {
            draw_feature(&mut img, base, tile.coord.zoom, feature);
        }
    }

    img
}

fn summarize_congestion(tiles: &[TrafficTile]) -> CongestionCounts {
    let mut counts = CongestionCounts::default();
    for tile in tiles {
        for feature in &tile.features {
            for seg in &feature.segments {
                match seg.congestion {
                    CongestionLevel::Normal => counts.normal += 1,
                    CongestionLevel::Slow => counts.slow += 1,
                    CongestionLevel::Heavy => counts.heavy += 1,
                    CongestionLevel::Severe => counts.severe += 1,
                    CongestionLevel::Unknown(_) => counts.unknown += 1,
                }
            }
        }
    }
    counts
}

fn draw_feature(img: &mut RgbaImage, base: &Basemap, zoom: u8, feature: &TrafficFeature) {
    if feature.geometry.len() < 2 {
        return;
    }

    let render_full_geom = std::env::var("RENDER_FULL_GEOM")
        .ok()
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(false);

    let points: Vec<(f64, f64)> = feature
        .geometry
        .iter()
        .map(|p| latlng_to_pixel(p, zoom, base.min_x, base.min_y))
        .collect();

    if render_full_geom {
        let color = congestion_color(feature.segments.first().map(|s| s.congestion).unwrap_or(
            match feature.layer_type % 10 {
                1 => CongestionLevel::Normal,
                2 => CongestionLevel::Slow,
                3 => CongestionLevel::Heavy,
                4 => CongestionLevel::Severe,
                _ => CongestionLevel::Unknown(0),
            },
        ));
        for i in 0..points.len().saturating_sub(1) {
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
        return;
    }

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

fn draw_overlay_panel(img: &mut RgbaImage, hour: u8, minute: u8) {
    let panel = Rect::at(10, 10).of_size(402, 92);
    draw_filled_rect_mut(img, panel, PANEL_BG);
    draw_hollow_rect_mut(img, panel, PANEL_BORDER);

    draw_text(img, 20, 18, "DTLA MONDAY TYPICAL", TEXT, 2);
    draw_text(img, 20, 40, "FULL DAY INTERP 5-MIN", TEXT, 2);
    draw_text(
        img,
        20,
        62,
        &format!("TIME {:02}:{:02}", hour, minute),
        TEXT,
        2,
    );

    let labels = [
        ("N", COLOR_NORMAL),
        ("S", COLOR_SLOW),
        ("H", COLOR_HEAVY),
        ("V", COLOR_SEVERE),
    ];

    let mut x = 304i32;
    for (label, color) in labels {
        draw_filled_rect_mut(img, Rect::at(x, 24).of_size(14, 14), color);
        draw_hollow_rect_mut(img, Rect::at(x, 24).of_size(14, 14), PANEL_BORDER);
        draw_text(img, x + 2, 44, label, TEXT, 2);
        x += 22;
    }
}

fn draw_text(img: &mut RgbaImage, x: i32, y: i32, text: &str, color: Rgba<u8>, scale: i32) {
    let mut cursor_x = x;
    for ch in text.chars() {
        if ch == ' ' {
            cursor_x += 4 * scale;
            continue;
        }
        if let Some(glyph) = font8x8::BASIC_FONTS.get(ch) {
            for (row, bits) in glyph.iter().enumerate() {
                for col in 0..8 {
                    if (bits >> col) & 1 == 1 {
                        let px = cursor_x + (col * scale as usize) as i32;
                        let py = y + (row * scale as usize) as i32;
                        draw_filled_rect_mut(
                            img,
                            Rect::at(px, py).of_size(scale as u32, scale as u32),
                            color,
                        );
                    }
                }
            }
        }
        cursor_x += 8 * scale + scale;
    }
}

fn blend_frames(a: &RgbaImage, b: &RgbaImage, alpha: f32) -> RgbaImage {
    let alpha = alpha.clamp(0.0, 1.0);
    let mut out = a.clone();
    let out_raw = out.as_mut();
    let a_raw = a.as_raw();
    let b_raw = b.as_raw();

    for i in (0..out_raw.len()).step_by(4) {
        out_raw[i] = lerp_u8(a_raw[i], b_raw[i], alpha);
        out_raw[i + 1] = lerp_u8(a_raw[i + 1], b_raw[i + 1], alpha);
        out_raw[i + 2] = lerp_u8(a_raw[i + 2], b_raw[i + 2], alpha);
        out_raw[i + 3] = 255;
    }

    out
}

fn lerp_u8(a: u8, b: u8, alpha: f32) -> u8 {
    let av = a as f32;
    let bv = b as f32;
    (av + (bv - av) * alpha).round().clamp(0.0, 255.0) as u8
}

fn write_gif_frame(
    encoder: &mut Encoder<&mut File>,
    frame_img: RgbaImage,
    delay_cs: u16,
) -> Result<(), Box<dyn Error>> {
    let width = frame_img.width() as u16;
    let height = frame_img.height() as u16;
    let mut raw = frame_img.into_raw();
    let mut frame = Frame::from_rgba_speed(width, height, &mut raw, 10);
    frame.delay = delay_cs;
    encoder.write_frame(&frame)?;
    Ok(())
}
