//! Export OSM road network as a graph with 24h typical congestion data.
//!
//! Builds an intersection graph from OSM data, fetches typical Monday traffic
//! for all 24 hours, matches it to OSM edges, and exports a JSON file for the
//! browser traffic simulator.
//!
//! Usage:
//!   cargo run --release --example export_graph --features osm -- <path/to.osm.pbf>
//!
//! Environment:
//!   OUTPUT=web/graph.json   Output path (default)
//!   BOUNDS=south,west,north,east   Override bounding box (default: greater LA)

use google_maps_traffic::{
    Bounds, CongestionLevel, DayOfWeek, LatLng, MatchConfig, Oneway, OsmRoadNetwork, TileCoord,
    TrafficClient,
};
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::error::Error;
use std::path::Path;

const ZOOM: u8 = 15;
const TILE_SIZE: u32 = 256;

const DEFAULT_BOUNDS: Bounds = Bounds {
    south: 33.70,
    west: -118.70,
    north: 34.35,
    east: -117.75,
};

fn parse_bounds() -> Bounds {
    match std::env::var("BOUNDS") {
        Ok(s) => {
            let parts: Vec<f64> = s.split(',').filter_map(|p| p.trim().parse().ok()).collect();
            if parts.len() == 4 {
                Bounds {
                    south: parts[0],
                    west: parts[1],
                    north: parts[2],
                    east: parts[3],
                }
            } else {
                eprintln!("Warning: BOUNDS must be south,west,north,east — using default");
                DEFAULT_BOUNDS
            }
        }
        Err(_) => DEFAULT_BOUNDS,
    }
}

// ── Canvas (Web Mercator projection, same as export_lanes) ──────────

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

// ── JSON output types ───────────────────────────────────────────────

#[derive(Serialize)]
struct GraphJson {
    w: u32,
    h: u32,
    nodes: Vec<NodeJson>,
    edges: Vec<EdgeJson>,
}

#[derive(Serialize)]
struct NodeJson {
    id: usize,
    x: f64,
    y: f64,
    lat: f64,
    lon: f64,
    light: bool,
    edges: Vec<usize>,
}

#[derive(Serialize)]
struct EdgeJson {
    id: usize,
    from: usize,
    to: usize,
    pts: Vec<[f64; 2]>,
    speed: f64,
    oneway: bool,
    congestion: [u8; 24],
}

fn congestion_u8(c: CongestionLevel) -> u8 {
    match c {
        CongestionLevel::Normal => 0,
        CongestionLevel::Slow => 1,
        CongestionLevel::Heavy => 2,
        CongestionLevel::Severe => 3,
        CongestionLevel::Unknown(_) => 0,
    }
}

// ── Internal graph edge ─────────────────────────────────────────────

struct GraphEdge {
    from_node: usize,
    to_node: usize,
    polyline: Vec<LatLng>,
    speed_kph: f64,
    oneway: bool,
    congestion: [u8; 24],
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let pbf_path = std::env::args().nth(1).ok_or_else(|| {
        "usage: cargo run --release --example export_graph --features osm -- <path/to.osm.pbf>"
            .to_string()
    })?;
    let output = std::env::var("OUTPUT").unwrap_or_else(|_| "web/graph.json".into());

    let bounds = parse_bounds();
    println!(
        "Bounds: {},{},{},{}",
        bounds.south, bounds.west, bounds.north, bounds.east
    );

    println!("Loading OSM network from: {pbf_path}");
    let network = OsmRoadNetwork::from_pbf_for_traffic_bounds(&pbf_path, &bounds, ZOOM)?;
    let osm_edges = network.edges();
    println!("Loaded {} OSM edges", osm_edges.len());

    let cv = Canvas::from_bounds(&bounds, ZOOM)?;
    println!("Canvas: {}x{}", cv.w, cv.h);

    // ── 1. Count node degrees ───────────────────────────────────────

    let mut degree: HashMap<i64, usize> = HashMap::new();
    let mut node_pos: HashMap<i64, LatLng> = HashMap::new();
    for e in osm_edges {
        *degree.entry(e.start_node_id).or_default() += 1;
        *degree.entry(e.end_node_id).or_default() += 1;
        node_pos.entry(e.start_node_id).or_insert(e.start);
        node_pos.entry(e.end_node_id).or_insert(e.end);
    }

    // ── 2. Group edges by way_id, split into contiguous chains ──────

    let mut way_edges_raw: HashMap<i64, Vec<usize>> = HashMap::new();
    for (i, e) in osm_edges.iter().enumerate() {
        way_edges_raw.entry(e.way_id).or_default().push(i);
    }

    let mut way_chains: Vec<(i64, Vec<Vec<usize>>)> = Vec::new();
    for (way_id, mut indices) in way_edges_raw {
        indices.sort_unstable();
        let mut chains: Vec<Vec<usize>> = Vec::new();
        let mut chain: Vec<usize> = vec![indices[0]];
        for i in 1..indices.len() {
            let prev_end = osm_edges[*chain.last().unwrap()].end_node_id;
            let curr_start = osm_edges[indices[i]].start_node_id;
            if prev_end == curr_start {
                chain.push(indices[i]);
            } else {
                chains.push(std::mem::take(&mut chain));
                chain.push(indices[i]);
            }
        }
        if !chain.is_empty() {
            chains.push(chain);
        }
        way_chains.push((way_id, chains));
    }
    way_chains.sort_by_key(|(wid, _)| *wid);

    // ── 3. Identify graph nodes (intersections + endpoints) ─────────

    let mut is_graph_node: HashSet<i64> = HashSet::new();

    for (&node_id, &deg) in &degree {
        if deg != 2 {
            is_graph_node.insert(node_id);
        }
    }
    for (_, chains) in &way_chains {
        for chain in chains {
            is_graph_node.insert(osm_edges[chain[0]].start_node_id);
            is_graph_node.insert(osm_edges[*chain.last().unwrap()].end_node_id);
        }
    }

    let mut graph_node_ids: Vec<i64> = is_graph_node.iter().copied().collect();
    graph_node_ids.sort_unstable();

    let mut graph_node_map: HashMap<i64, usize> = HashMap::new();
    let mut graph_node_data: Vec<(LatLng, usize)> = Vec::new();
    for &nid in &graph_node_ids {
        graph_node_map.insert(nid, graph_node_data.len());
        graph_node_data.push((node_pos[&nid], degree[&nid]));
    }

    println!(
        "{} graph nodes ({} with traffic lights)",
        graph_node_data.len(),
        graph_node_data.iter().filter(|(_, d)| *d >= 3).count()
    );

    // ── 4. Build graph edges (merge OsmEdges between graph nodes) ───

    let mut graph_edges: Vec<GraphEdge> = Vec::new();
    let mut osm_to_graph: HashMap<(i64, i64, i64), usize> = HashMap::new();

    for &(way_id, ref chains) in &way_chains {
        for chain in chains {
            let mut poly: Vec<LatLng> = Vec::new();
            let mut from_gn: Option<usize> = None;
            let mut seg_start: usize = 0;
            let mut speed_sum = 0.0f64;
            let mut speed_n = 0usize;
            let mut oneway = false;

            for (ci, &ei) in chain.iter().enumerate() {
                let e = &osm_edges[ei];

                if poly.is_empty() {
                    poly.push(e.start);
                    from_gn = graph_node_map.get(&e.start_node_id).copied();
                    oneway = !matches!(e.oneway, Oneway::No);
                    seg_start = ci;
                }

                poly.push(e.end);
                speed_sum += e.max_speed_kph;
                speed_n += 1;

                if is_graph_node.contains(&e.end_node_id) {
                    if let (Some(fi), Some(&ti)) =
                        (from_gn, graph_node_map.get(&e.end_node_id))
                    {
                        if fi != ti || poly.len() > 2 {
                            let ge_idx = graph_edges.len();
                            for &ej in &chain[seg_start..=ci] {
                                let oe = &osm_edges[ej];
                                osm_to_graph.insert(
                                    (way_id, oe.start_node_id, oe.end_node_id),
                                    ge_idx,
                                );
                            }
                            graph_edges.push(GraphEdge {
                                from_node: fi,
                                to_node: ti,
                                polyline: std::mem::take(&mut poly),
                                speed_kph: if speed_n > 0 {
                                    speed_sum / speed_n as f64
                                } else {
                                    40.0
                                },
                                oneway,
                                congestion: [0; 24],
                            });
                        }
                    }
                    poly.clear();
                    speed_sum = 0.0;
                    speed_n = 0;
                    from_gn = None;
                }
            }
        }
    }
    println!("{} graph edges", graph_edges.len());

    // ── 5. Fetch 24h typical traffic & match ────────────────────────

    let client = TrafficClient::new()?;
    let match_config = MatchConfig::for_zoom(ZOOM);

    println!("Fetching typical Monday traffic for 24 hours...");
    for hour in 0..24u8 {
        let tiles = client
            .get_typical_traffic(&bounds, ZOOM, DayOfWeek::Monday, hour)
            .await?;
        let mo = network.match_tiles(&tiles, &match_config);

        let mut mapped = 0usize;
        for m in &mo.matches {
            let key = (m.edge.way_id, m.edge.start_node_id, m.edge.end_node_id);
            if let Some(&gi) = osm_to_graph.get(&key) {
                let c = congestion_u8(m.congestion);
                graph_edges[gi].congestion[hour as usize] =
                    graph_edges[gi].congestion[hour as usize].max(c);
                mapped += 1;
            }
        }
        println!(
            "  {:02}:00  {} matched → {} mapped",
            hour,
            mo.matches.len(),
            mapped
        );
    }

    // ── 6. Build node→edge adjacency lists ──────────────────────────

    let mut node_edges: Vec<Vec<usize>> = vec![Vec::new(); graph_node_data.len()];
    for (i, ge) in graph_edges.iter().enumerate() {
        node_edges[ge.from_node].push(i);
        node_edges[ge.to_node].push(i);
    }
    for v in &mut node_edges {
        v.sort_unstable();
        v.dedup();
    }

    // ── 7. Serialize & write ────────────────────────────────────────

    let nodes_json: Vec<NodeJson> = graph_node_data
        .iter()
        .enumerate()
        .map(|(i, (pos, deg))| {
            let (x, y) = cv.project(pos);
            NodeJson {
                id: i,
                x: (x * 10.0).round() / 10.0,
                y: (y * 10.0).round() / 10.0,
                lat: (pos.lat * 1e6).round() / 1e6,
                lon: (pos.lng * 1e6).round() / 1e6,
                edges: node_edges[i].clone(),
                light: *deg >= 3,
            }
        })
        .collect();

    let edges_json: Vec<EdgeJson> = graph_edges
        .iter()
        .enumerate()
        .map(|(i, ge)| {
            let pts: Vec<[f64; 2]> = ge
                .polyline
                .iter()
                .map(|p| {
                    let (x, y) = cv.project(p);
                    [(x * 10.0).round() / 10.0, (y * 10.0).round() / 10.0]
                })
                .collect();
            EdgeJson {
                id: i,
                from: ge.from_node,
                to: ge.to_node,
                pts,
                speed: (ge.speed_kph * 10.0).round() / 10.0,
                oneway: ge.oneway,
                congestion: ge.congestion,
            }
        })
        .collect();

    let graph = GraphJson {
        w: cv.w,
        h: cv.h,
        nodes: nodes_json,
        edges: edges_json,
    };

    if let Some(p) = Path::new(&output).parent().filter(|p| !p.as_os_str().is_empty()) {
        std::fs::create_dir_all(p)?;
    }

    let json = serde_json::to_string(&graph)?;
    std::fs::write(&output, &json)?;
    println!(
        "\nWrote {} ({:.0} KB)",
        output,
        json.len() as f64 / 1024.0
    );
    println!("  {} nodes, {} edges", graph.nodes.len(), graph.edges.len());
    println!("\nnext: cd web && python3 -m http.server 8080");

    Ok(())
}
