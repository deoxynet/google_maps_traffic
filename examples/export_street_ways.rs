//! Export street ways (with one-way, name and parking tags) inside a bounding box, for the parking simulator's
//! routing graph.
//!
//! One pass over the PBF: node coordinates inside the box (plus a margin) are kept, then every highway way that
//! touches the box is written with its tags and node list. Relies on the usual PBF order (nodes before ways).
//!
//! Usage:
//!   BOUNDS=34.029,-118.351,34.0785,-118.274 OUTPUT=data/parking_sim/osm_ways.json \
//!     cargo run --release --example export_street_ways --features osm -- socal.osm.pbf

use osmpbf::{Element, ElementReader};
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};
use std::error::Error;
use std::io::{BufWriter, Write};

const HIGHWAYS: &[&str] = &[
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
    "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street",
];
const NODE_MARGIN: f64 = 0.01;

#[derive(Serialize)]
struct Way {
    id: i64,
    highway: String,
    tags: BTreeMap<String, String>,
    nodes: Vec<(i64, f64, f64)>,
}

#[derive(Serialize)]
struct Output {
    bounds: [f64; 4],
    ways: Vec<Way>,
}

fn main() -> Result<(), Box<dyn Error>> {
    let pbf = std::env::args().nth(1).ok_or("usage: export_street_ways <path.osm.pbf>")?;
    let b: Vec<f64> = std::env::var("BOUNDS")?.split(',').map(|s| s.trim().parse()).collect::<Result<_, _>>()?;
    let (s, w, n, e) = (b[0], b[1], b[2], b[3]);
    let output = std::env::var("OUTPUT").unwrap_or_else(|_| "data/parking_sim/osm_ways.json".into());
    let inside = |lat: f64, lon: f64, m: f64| lat >= s - m && lat <= n + m && lon >= w - m && lon <= e + m;

    let mut coords: HashMap<i64, (f64, f64)> = HashMap::new();
    let mut ways = Vec::new();
    ElementReader::from_path(&pbf)?.for_each(|el| match el {
        Element::Node(nd) => {
            if inside(nd.lat(), nd.lon(), NODE_MARGIN) {
                coords.insert(nd.id(), (nd.lat(), nd.lon()));
            }
        }
        Element::DenseNode(nd) => {
            if inside(nd.lat(), nd.lon(), NODE_MARGIN) {
                coords.insert(nd.id(), (nd.lat(), nd.lon()));
            }
        }
        Element::Way(way) => {
            let mut highway = None;
            let mut tags = BTreeMap::new();
            for (k, v) in way.tags() {
                if k == "highway" {
                    highway = Some(v.to_string());
                }
                if k == "oneway" || k == "name" || k == "junction" || k == "lanes" || k.starts_with("parking:") {
                    tags.insert(k.to_string(), v.to_string());
                }
            }
            let Some(hw) = highway.filter(|h| HIGHWAYS.contains(&h.as_str())) else { return };
            let nodes: Vec<(i64, f64, f64)> = way
                .refs()
                .filter_map(|id| coords.get(&id).map(|&(la, lo)| (id, la, lo)))
                .collect();
            if nodes.len() >= 2 && nodes.iter().any(|&(_, la, lo)| inside(la, lo, 0.0)) {
                ways.push(Way { id: way.id(), highway: hw, tags, nodes });
            }
        }
        _ => {}
    })?;

    if let Some(dir) = std::path::Path::new(&output).parent() {
        std::fs::create_dir_all(dir)?;
    }
    let mut out = BufWriter::new(std::fs::File::create(&output)?);
    serde_json::to_writer(&mut out, &Output { bounds: [s, w, n, e], ways: ways.iter().map(|w| Way {
        id: w.id, highway: w.highway.clone(), tags: w.tags.clone(), nodes: w.nodes.clone() }).collect() })?;
    out.flush()?;
    let parking = ways.iter().filter(|w| w.tags.keys().any(|k| k.starts_with("parking:"))).count();
    let oneway = ways.iter().filter(|w| w.tags.get("oneway").map_or(false, |v| v != "no")).count();
    eprintln!("{} ways ({} one-way, {} with parking tags), {} nodes kept -> {}", ways.len(), oneway, parking, coords.len(), output);
    Ok(())
}
