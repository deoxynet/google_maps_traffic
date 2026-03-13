# web-sim — DTLA Traffic Particle Simulator

Real-time WebGL2 traffic visualization for the DTLA road network.
Rust/WASM simulation with 110K routed car spawns across a 24-hour cycle,
driven by census commute data, land use trip generation, and traffic sensor calibration.

![architecture](../docs/assets/dtla_osm_typical_monday_full_day_z15_5min_preview.gif)

## Quick start

```bash
cd web-sim && wasm-pack build --target web --release --out-dir ../web/pkg
cd ../web && python3 serve.py
# open http://localhost:8080
```

`serve.py` serves pre-compressed `graph.json.gz` (7 MB) instead of the raw 41 MB file.

## Architecture

```
web-sim/src/
├── lib.rs              Simulator struct, wasm_bindgen JS API
├── graph.rs            Graph JSON parsing, packed SoA runtime representation
├── sim.rs              Particle simulation: SoA state, tick(), spawn/transfer/despawn
├── render.rs           WebGL2 renderer: 4-pass pipeline (roads, particles, bloom, composite)
├── bloom.rs            Half-resolution ping-pong Gaussian blur
├── road_mesh.rs        Static road network VBO (GL_LINES, built once)
├── particle_mesh.rs    Dynamic particle trail VBO (updated per frame)
├── shaders.rs          All GLSL source as const &str (6 programs)
└── math.rs             xorshift32 RNG, interpolation helpers

web/
├── index.html          Thin JS glue (~80 lines), same UI panels/sliders
├── graph.json          Road network + routes (41 MB, 7 MB gzipped)
├── serve.py            HTTP server with gzip content-encoding support
└── pkg/                wasm-pack output (WASM binary + JS bindings)
```

### Rendering pipeline (per frame)

| Pass | Target | What |
|------|--------|------|
| 1 | Default framebuffer | Clear to `rgb(3,3,12)` + draw road network (GL_LINES, alpha blend) |
| 2 | Particle FBO | Draw car trails (GL_LINES) + head dots (GL_POINTS), additive blend |
| 3 | Half-res ping-pong FBOs | 9-tap separable Gaussian blur (horizontal then vertical) |
| 4 | Default framebuffer | Composite: particles + bloom × mix, additive over roads |

### Simulation

SoA (Structure-of-Arrays) particle state for cache-friendly iteration over 50K+ cars.
Each car has: edge, direction, t (position along edge), speed, jitter, route index, route step.

Per tick:
1. Advance sim clock (24h cycle, configurable speed)
2. Spawn route cars whose departure time has passed
3. Move all cars: `speed = edge_speed_limit × congestion_factor × per_car_jitter`
4. Transfer cars at intersections (bearing-weighted random walk for ambient cars, deterministic for routed cars)
5. Maintain ambient population target (spawn/despawn to match time-of-day curve)
6. Compute head + tail positions for renderer

## WASM ↔ JS interface

```rust
#[wasm_bindgen]
impl Simulator {
    fn new(canvas_id: &str, graph_json: &str) -> Result<Simulator, JsValue>;
    fn frame(&mut self) -> FrameStats;                // tick + render
    fn set_param(&mut self, name: &str, value: f32);  // slider changes
    fn resize(&mut self, width: u32, height: u32);
}
```

JS calls `sim.frame()` in a `requestAnimationFrame` loop and reads `FrameStats { car_count, sim_hour, sim_minute }` to update DOM.

## Data pipeline

### graph.json format

```json
{
  "w": 4200, "h": 3200,
  "nodes": [{"id": 0, "x": 100, "y": 200, "lat": 34.05, "lon": -118.25, "light": true, "edges": [0,1]}],
  "edges": [{"from": 0, "to": 1, "pts": [[x,y]...], "speed": 50, "oneway": false, "congestion": [0..23]}],
  "paths": [{"edges": [0,1,2], "dirs": [1,1,-1]}],
  "routes": [{"p": 0, "d": 7.5, "c": 3}]
}
```

Routes reference deduplicated paths by index (`p`), with departure hour (`d`) and car count (`c`).
This deduplication reduces the file from 68 MB to 41 MB (26K unique paths for 110K route entries).

### Three route sources

| Source | Routes | Description |
|--------|--------|-------------|
| PeMS + NavigateLA (LP) | 30,000 | Traffic sensor calibrated. LP solver decomposes observed ramp/intersection counts into routes. |
| LODES census OD (commute) | 60,000 | Census home→work block-level flows. Dijkstra-routed on OSM graph, Gaussian departure times. |
| Parcel land use (non-work) | 20,000 | ITE trip rates × urban infill factors × gravity-model destinations. Shopping, errands, school, social. |

Daily car spawn distribution:

```
 07:00  ██████████████████ 10K  (morning commute peak)
 12:00  ██████ 3.5K              (midday errands)
 16:00  ██████████████████ 10K  (evening commute peak)
 03:00  ██ 1.3K                  (overnight minimum)
```

## Data sources and processing

All raw data lives in `data/lodes/`. Processing scripts are inline Python (run once, output cached as JSON/CSV).

### LEHD/LODES (Census Bureau)

Origin-destination employment statistics at census block level.

| File | Source | Description |
|------|--------|-------------|
| `ca_od_main_JT00_2023.csv.gz` | [LODES8](https://lehd.ces.census.gov/data/lodes/LODES8/ca/od/) | All CA home→work OD pairs (16M rows, 93 MB) |
| `ca_xwalk.csv.gz` | [LODES8](https://lehd.ces.census.gov/data/lodes/LODES8/ca/) | Census block FIPS → lat/lon crosswalk |
| `la_blocks.csv` | Derived | 83K blocks in wider LA area (7K core, 76K commute shed) |
| `la_od_filtered.csv` | Derived | 674K OD pairs (728K workers) touching graph area |
| `node_od_full.csv` | Derived | 206K node-to-node OD pairs (719K workers, boundary-routed) |
| `lodes_routes_fullday.json` | Derived | 60K routes: 30K morning + 30K return trips |

Processing pipeline:
1. Filter CA OD file to flows where home or work is within graph bounds (+ 30km buffer)
2. Map blocks to nearest graph node (core blocks: median 89m, external blocks: nearest boundary node)
3. Dijkstra shortest-path routing on OSM graph
4. Departure times: morning = N(7.5, 1.2²) clamped 5-10h; return = departure + N(9, 1²)

### ACS commute data (Census Bureau)

Travel time to work by census tract (table B08303).

| File | Source |
|------|--------|
| `acs_b08303_la.json` | [Census API](https://api.census.gov/data/2022/acs/acs5?get=NAME,B08303_001E,...&for=tract:*&in=state:06%20county:037) |
| `acs_commute_by_tract.json` | Derived — median commute time per tract |

2,498 LA County tracts. Median commute: 30-34 minutes for most tracts.
Used for calibrating route travel times against observed commute durations.

### LA County Assessor parcel data

Land use classification for trip generation.

| File | Source |
|------|--------|
| `dtla_parcels.csv` | [LA County GIS](https://public.gis.lacounty.gov/public/rest/services/LACounty_Cache/LACounty_Parcel/MapServer/0) |

88K parcels in graph area:
- 76,612 Residential
- 7,141 Commercial
- 2,191 Industrial
- 666 Institutional
- 627 Government
- 111 Recreational

Non-work trip generation:
- ITE Trip Generation rates per land use category, adjusted by Caltrans urban infill factors (0.56× residential, 0.51× office, 0.70× retail)
- Gravity-model destination selection: residential origins → commercial/institutional attractors
- NHTS-derived time-of-day distribution (peaked at 3-6pm for shopping/errands)

### PeMS + NavigateLA (existing)

| File | Source |
|------|--------|
| `data/pems.db` | [Caltrans PeMS](https://pems.dot.ca.gov/) — freeway ramp 5-min volume counts |
| `data/navigatela.db` | [NavigateLA](https://navigatela.lacity.org/) — intersection turning movement counts |

LP solver (`scripts/build_routes.py`) decomposes observed counts into 30K routes using scipy's HiGHS linprog + BPR volume-delay function.

## Speed model

Each car's speed is determined per-tick:

```
speed = speed_base × (edge_speed_limit / 50) × congestion_factor × car_jitter
```

- `speed_base`: global multiplier from speed slider
- `edge_speed_limit`: OSM-derived speed (km/h), normalized to reference 50 km/h
- `congestion_factor`: interpolated from Google Maps congestion levels 0-3 → [0.95, 0.70, 0.45, 0.20]
- `car_jitter`: per-car random multiplier in [0.8, 1.2], set once at spawn, persists across edge transitions

## Build

Requires [wasm-pack](https://rustwasm.github.io/wasm-pack/installer/):

```bash
# Development (fast compile, no optimizations)
wasm-pack build --target web --dev --out-dir ../web/pkg

# Release (optimized, wasm-opt applied)
wasm-pack build --target web --release --out-dir ../web/pkg
```

## Tests

```bash
cargo test -p web-sim
```

10 tests covering RNG, angle math, graph position interpolation, congestion factors, and clock initialization.
