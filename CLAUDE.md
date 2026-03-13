# CLAUDE.md

## Build & Deploy Protocol

After any change to Rust sim code or graph data, run this full sequence:

```bash
# 1. Run any enrichment scripts that changed graph.json
python3 scripts/enrich_parking.py   # (or whichever script)

# 2. Build WASM
wasm-pack build web-sim --target web --release

# 3. Copy WASM artifacts to web/pkg/
cp web-sim/pkg/web_sim_bg.wasm web-sim/pkg/web_sim.js web-sim/pkg/web_sim.d.ts web/pkg/

# 4. Re-compress graph.json (serve.py serves .gz preferentially!)
gzip -kf web/graph.json
```

**Critical**: `web/serve.py` serves `graph.json.gz` over `graph.json` when the browser accepts gzip. If you update `graph.json` without recompressing, the browser gets stale data. Always run step 4 after touching graph.json.

## Project Structure

- `web-sim/src/` — Rust WASM simulation (graph, sim, render, lib)
- `web/` — Frontend (index.html, graph.json, pkg/)
- `web/serve.py` — Dev server with gzip support (port 8080)
- `scripts/` — Python data enrichment pipelines

## Architecture

SoA (struct-of-arrays) layout throughout. Graph is flat packed arrays with CSR adjacency. Simulation runs N physics ticks per frame, then renders via WebGL2 with bloom compositing.

### Key data flow
1. `scripts/` enrich `web/graph.json` with real-world data (signals, turns, lanes, parking)
2. `web-sim/src/graph.rs` deserializes JSON into flat arrays
3. `web-sim/src/sim.rs` runs physics (car-following, signals, gravity, parking)
4. `web-sim/src/render.rs` draws via WebGL2 (roads → particles FBO → bloom → composite)
5. `web-sim/src/lib.rs` bridges WASM ↔ JS (FrameStats, sliders, camera, gravity API)

## Testing

```bash
cargo test -p web-sim   # unit tests (graph parsing, sim math)
```

Browser test: open `http://localhost:8080` after running serve.py from `web/`.
