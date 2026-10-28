use serde::Deserialize;

/// Meters per pixel in the graph coordinate system (calibrated to DTLA tile scale).
pub const METERS_PER_PIXEL: f32 = 3.9476;

// ── JSON deserialization types ──────────────────────────────────────

#[derive(Deserialize)]
#[allow(dead_code)]
pub struct GraphJson {
    pub w: u32,
    pub h: u32,
    pub nodes: Vec<NodeJson>,
    pub edges: Vec<EdgeJson>,
    #[serde(default)]
    pub routes: Vec<RouteJson>,
    /// Deduplicated paths (new compact format)
    #[serde(default)]
    pub paths: Vec<PathJson>,
    /// Data-driven hourly volume profile (24 floats, normalized to [0,1])
    #[serde(default)]
    pub hourly_volume: Vec<f32>,
    /// Total counted peak-hour volume across all matched nodes
    #[serde(default)]
    pub total_counted_volume: f32,
    /// Data-driven peak instantaneous car count (from Little's Law)
    #[serde(default)]
    pub peak_cars: f32,
}

#[derive(Deserialize)]
pub struct NodeJson {
    pub id: u32,
    pub x: f32,
    pub y: f32,
    pub light: bool,
    pub edges: Vec<u32>,
    /// Stop sign control (from OSM)
    #[serde(default)]
    pub stop: bool,
    /// Signal timing: [AM_cycle, AM_ns_green, PM_cycle, PM_ns_green,
    ///                  MID_cycle, MID_ns_green, OFF_cycle, OFF_ns_green]
    #[serde(default)]
    pub signal: Option<Vec<f32>>,
    /// Turn ratios: [EB_L,EB_T,EB_R, WB_L,WB_T,WB_R, NB_L,NB_T,NB_R, SB_L,SB_T,SB_R]
    #[serde(default)]
    pub turns: Option<Vec<f32>>,
    /// Peak hourly volume
    #[serde(default)]
    pub vol: Option<f32>,
    /// Parking capacity (spaces at this node)
    #[serde(default)]
    pub parking: Option<u32>,
}

#[derive(Deserialize)]
#[allow(dead_code)]
pub struct EdgeJson {
    pub id: Option<u32>,
    pub from: u32,
    pub to: u32,
    pub pts: Vec<[f32; 2]>,
    pub speed: f32,
    pub oneway: bool,
    pub congestion: Vec<f32>,
    #[serde(default = "default_lanes")]
    pub lanes: u8,
}

fn default_lanes() -> u8 {
    1
}

/// Old format: inline edges/dirs. New compact format: path index reference.
#[derive(Deserialize)]
#[allow(dead_code)]
pub struct RouteJson {
    /// Inline edge list (old format)
    #[serde(default)]
    pub edges: Vec<u32>,
    /// Inline dir list (old format)
    #[serde(default)]
    pub dirs: Vec<i8>,
    /// Departure time — either "depart" (old) or "d" (compact)
    #[serde(default)]
    pub depart: f32,
    #[serde(default)]
    pub d: Option<f32>,
    /// Count — either "count" (old) or "c" (compact)
    #[serde(default = "default_count")]
    pub count: u32,
    #[serde(default)]
    pub c: Option<u32>,
    /// Path index reference (compact format)
    #[serde(default)]
    pub p: Option<usize>,
}

#[derive(Deserialize)]
#[allow(dead_code)]
pub struct PathJson {
    pub edges: Vec<u32>,
    pub dirs: Vec<i8>,
}

#[allow(dead_code)]
fn default_count() -> u32 {
    1
}

// ── Packed runtime graph (SoA, cache-friendly) ──────────────────────

#[allow(dead_code)]
pub struct Graph {
    pub width: u32,
    pub height: u32,

    // Node data
    pub node_count: u32,
    pub node_x: Vec<f32>,
    pub node_y: Vec<f32>,
    pub node_light: Vec<bool>,
    /// CSR adjacency: node_edges_offset[i]..node_edges_offset[i+1] indexes into node_edges_data
    pub node_edges_offset: Vec<u32>,
    pub node_edges_data: Vec<u32>,

    // Stop sign nodes
    pub node_stop: Vec<bool>,

    // Edge data (SoA)
    pub edge_count: u32,
    pub edge_from: Vec<u32>,
    pub edge_to: Vec<u32>,
    pub edge_speed: Vec<f32>,
    pub edge_oneway: Vec<bool>,
    /// 24 congestion values per edge, packed flat: edge i → [i*24 .. i*24+24)
    pub edge_congestion: Vec<f32>,
    pub edge_total_len: Vec<f32>,
    pub edge_lanes: Vec<u8>,

    // Polyline points (packed): edge i → pts_offset[i]..pts_offset[i+1]
    pub pts_x: Vec<f32>,
    pub pts_y: Vec<f32>,
    pub pts_offset: Vec<u32>,

    // Cumulative distance along each edge's polyline (packed same as pts)
    pub cum_dist: Vec<f32>,

    // Ambient congestion pattern (normalized per hour)
    pub hour_weight: [f32; 24],

    // Signal timing: flat array of node_count * 8 floats (0 = no signal)
    pub node_signal: Vec<f32>,
    // Indices of nodes that have signal data (for visualization)
    pub signal_nodes: Vec<u32>,

    // Turn ratios: flat [node_count * 12], zero = no data
    // Layout: [EB_L,EB_T,EB_R, WB_L,WB_T,WB_R, NB_L,NB_T,NB_R, SB_L,SB_T,SB_R]
    pub node_turns: Vec<f32>,
    // Peak hourly volume per node (0 = uncounted)
    pub node_volume: Vec<f32>,
    // Indices of nodes with turn data
    pub counted_nodes: Vec<u32>,

    // Precomputed edge bearings
    pub edge_bearing_fwd: Vec<f32>, // arrival bearing at edge_to (dir=+1)
    pub edge_bearing_rev: Vec<f32>, // arrival bearing at edge_from (dir=-1)

    // Parking capacity per node (0 = no parking)
    pub node_parking: Vec<u32>,
    // Indices of nodes that have parking capacity > 0
    pub parking_nodes: Vec<u32>,

    // Spawn probability: cumulative distribution for weighted random edge selection
    pub edge_spawn_cdf: Vec<f32>, // cumulative, length = edge_count

    // Data-driven hourly volume profile from manual counts (24 floats, normalized to [0,1])
    pub hourly_volume: [f32; 24],
    // Total counted peak-hour volume across matched nodes
    pub total_counted_volume: f32,
    // Data-driven peak instantaneous car count (from Little's Law on counted volumes)
    pub peak_cars: f32,
}

impl Graph {
    pub fn from_json(json_str: &str) -> Result<Self, String> {
        let data: GraphJson = serde_json::from_str(json_str).map_err(|e| e.to_string())?;
        Self::from_parsed(data)
    }

    pub fn from_parsed(data: GraphJson) -> Result<Self, String> {
        let node_count = data.nodes.len() as u32;
        let edge_count = data.edges.len() as u32;

        // ── Compute ambient congestion weights ──
        let mut hour_sum = [0.0f64; 24];
        let mut hour_n = [0u32; 24];
        for e in &data.edges {
            let has_data = e.congestion.iter().any(|&c| c > 0.0);
            if !has_data {
                continue;
            }
            for h in 0..24 {
                hour_sum[h] += e.congestion[h] as f64;
                hour_n[h] += 1;
            }
        }
        let mut hour_weight = [0.0f32; 24];
        let mut max_avg: f64 = 0.0;
        for h in 0..24 {
            if hour_n[h] > 0 {
                hour_weight[h] = (hour_sum[h] / hour_n[h] as f64) as f32;
            }
            if (hour_weight[h] as f64) > max_avg {
                max_avg = hour_weight[h] as f64;
            }
        }
        if max_avg > 0.0 {
            for h in 0..24 {
                hour_weight[h] /= max_avg as f32;
            }
        }

        // ── Pack nodes ──
        let mut node_x = vec![0.0f32; node_count as usize];
        let mut node_y = vec![0.0f32; node_count as usize];
        let mut node_light = vec![false; node_count as usize];
        let mut node_edges_offset = vec![0u32; node_count as usize + 1];
        let mut node_edges_data = Vec::new();

        let mut node_stop = vec![false; node_count as usize];

        let mut node_signal = vec![0.0f32; node_count as usize * 8];
        let mut signal_nodes = Vec::new();
        let mut node_turns = vec![0.0f32; node_count as usize * 12];
        let mut node_volume = vec![0.0f32; node_count as usize];
        let mut counted_nodes = Vec::new();
        let mut node_parking = vec![0u32; node_count as usize];
        let mut parking_nodes = Vec::new();

        for n in &data.nodes {
            let i = n.id as usize;
            if i >= node_count as usize {
                continue;
            }
            node_x[i] = n.x;
            node_y[i] = n.y;
            node_light[i] = n.light;
            node_stop[i] = n.stop;

            // Unpack signal timing if present
            if let Some(ref sig) = n.signal {
                if sig.len() >= 8 {
                    let base = i * 8;
                    for j in 0..8 {
                        node_signal[base + j] = sig[j];
                    }
                    signal_nodes.push(i as u32);
                }
            }

            // Unpack turn ratios if present
            if let Some(ref turns) = n.turns {
                if turns.len() >= 12 {
                    let base = i * 12;
                    let mut has_data = false;
                    for j in 0..12 {
                        node_turns[base + j] = turns[j];
                        if turns[j] > 0.0 {
                            has_data = true;
                        }
                    }
                    if has_data {
                        if let Some(vol) = n.vol {
                            node_volume[i] = vol;
                        }
                        counted_nodes.push(i as u32);
                    }
                }
            }

            // Unpack parking capacity
            if let Some(p) = n.parking {
                if p > 0 {
                    node_parking[i] = p;
                    parking_nodes.push(i as u32);
                }
            }
        }
        // Build CSR adjacency
        // First pass: count edges per node
        let mut counts = vec![0u32; node_count as usize];
        for n in &data.nodes {
            let i = n.id as usize;
            if i < node_count as usize {
                counts[i] = n.edges.len() as u32;
            }
        }
        // Prefix sum
        node_edges_offset[0] = 0;
        for i in 0..node_count as usize {
            node_edges_offset[i + 1] = node_edges_offset[i] + counts[i];
        }
        // Fill data
        node_edges_data.resize(node_edges_offset[node_count as usize] as usize, 0);
        let mut cursors = vec![0u32; node_count as usize];
        for n in &data.nodes {
            let i = n.id as usize;
            if i >= node_count as usize {
                continue;
            }
            for &eid in &n.edges {
                let pos = node_edges_offset[i] + cursors[i];
                node_edges_data[pos as usize] = eid;
                cursors[i] += 1;
            }
        }

        // ── Pack edges ──
        let mut edge_from = vec![0u32; edge_count as usize];
        let mut edge_to = vec![0u32; edge_count as usize];
        let mut edge_speed = vec![0.0f32; edge_count as usize];
        let mut edge_oneway = vec![false; edge_count as usize];
        let mut edge_congestion = vec![0.0f32; edge_count as usize * 24];
        let mut edge_total_len = vec![0.0f32; edge_count as usize];
        let mut edge_lanes = vec![1u8; edge_count as usize];

        let mut pts_x = Vec::new();
        let mut pts_y = Vec::new();
        let mut pts_offset = vec![0u32; edge_count as usize + 1];
        let mut cum_dist = Vec::new();

        for (idx, e) in data.edges.iter().enumerate() {
            edge_from[idx] = e.from;
            edge_to[idx] = e.to;
            edge_speed[idx] = e.speed;
            edge_oneway[idx] = e.oneway;
            edge_lanes[idx] = e.lanes.max(1);

            // Polyline points
            let pt_start = pts_x.len() as u32;
            let mut cd = Vec::with_capacity(e.pts.len());
            cd.push(0.0f32);
            for (j, pt) in e.pts.iter().enumerate() {
                pts_x.push(pt[0]);
                pts_y.push(pt[1]);
                if j > 0 {
                    let dx = e.pts[j][0] - e.pts[j - 1][0];
                    let dy = e.pts[j][1] - e.pts[j - 1][1];
                    cd.push(cd[j - 1] + (dx * dx + dy * dy).sqrt());
                }
            }
            let total = *cd.last().unwrap_or(&0.0);
            edge_total_len[idx] = total;
            pts_offset[idx] = pt_start;
            cum_dist.extend_from_slice(&cd);

            // Congestion — fill ambient for edges with no data
            let has_data = e.congestion.iter().any(|&c| c > 0.0);
            let base = idx * 24;
            if has_data {
                for h in 0..24 {
                    edge_congestion[base + h] = e.congestion[h];
                }
            } else {
                let road_factor = (1.0 - e.speed / 100.0).max(0.3);
                for h in 0..24 {
                    edge_congestion[base + h] = (hour_weight[h] * road_factor * 2.5).min(3.0);
                }
            }
        }
        pts_offset[edge_count as usize] = pts_x.len() as u32;

        // Build a temporary Graph to use edge_bearing() for precomputation
        // (we need pts data in place first)
        let mut graph = Graph {
            width: data.w,
            height: data.h,
            node_count,
            node_x,
            node_y,
            node_light,
            node_stop,
            node_edges_offset,
            node_edges_data,
            edge_count,
            edge_from,
            edge_to,
            edge_speed,
            edge_oneway,
            edge_congestion,
            edge_total_len,
            edge_lanes,
            pts_x,
            pts_y,
            pts_offset,
            cum_dist,
            hour_weight,
            node_signal,
            signal_nodes,
            node_turns,
            node_volume,
            counted_nodes,
            node_parking,
            parking_nodes,
            edge_bearing_fwd: Vec::new(),
            edge_bearing_rev: Vec::new(),
            edge_spawn_cdf: Vec::new(),
            hourly_volume: [0.0; 24],
            total_counted_volume: 0.0,
            peak_cars: 0.0,
        };

        // ── Precompute edge bearings ──
        let ec = edge_count as usize;
        let mut edge_bearing_fwd = vec![0.0f32; ec];
        let mut edge_bearing_rev = vec![0.0f32; ec];
        for i in 0..ec {
            edge_bearing_fwd[i] = graph.edge_bearing(i as u32, 1, true);
            edge_bearing_rev[i] = graph.edge_bearing(i as u32, -1, true);
        }
        graph.edge_bearing_fwd = edge_bearing_fwd;
        graph.edge_bearing_rev = edge_bearing_rev;

        // ── Precompute spawn CDF ──
        // Weight = speed * length * (1 + nearby_volume_boost)
        let mut edge_spawn_cdf = vec![0.0f32; ec];
        let mut cum = 0.0f32;
        for i in 0..ec {
            let speed = graph.edge_speed[i];
            let len = graph.edge_total_len[i];
            // Boost edges near counted intersections
            let to_vol = graph.node_volume[graph.edge_to[i] as usize];
            let from_vol = graph.node_volume[graph.edge_from[i] as usize];
            let vol_boost = 1.0 + (to_vol + from_vol).min(5000.0) / 2000.0;
            let w = speed * len * vol_boost;
            cum += w;
            edge_spawn_cdf[i] = cum;
        }
        // Normalize to [0, 1]
        if cum > 0.0 {
            for v in edge_spawn_cdf.iter_mut() {
                *v /= cum;
            }
        }
        graph.edge_spawn_cdf = edge_spawn_cdf;

        // ── Parse hourly volume profile ──
        if data.hourly_volume.len() >= 24 {
            for h in 0..24 {
                graph.hourly_volume[h] = data.hourly_volume[h];
            }
        }
        graph.total_counted_volume = data.total_counted_volume;
        graph.peak_cars = data.peak_cars;

        Ok(graph)
    }

    /// Interpolate position along edge polyline. t in [0, 1].
    /// Returns (x, y).
    #[inline(always)]
    #[allow(dead_code)]
    pub fn edge_pos(&self, ei: u32, t: f32) -> (f32, f32) {
        let t = t.clamp(0.0, 1.0);
        let total = self.edge_total_len[ei as usize];
        let d = t * total;

        let off = self.pts_offset[ei as usize] as usize;
        let end = self.pts_offset[ei as usize + 1] as usize;
        let n = end - off;
        if n < 2 {
            return (self.pts_x[off], self.pts_y[off]);
        }

        // Binary search on cum_dist[off..end]
        let cum = &self.cum_dist[off..end];
        let mut lo = 1usize;
        let mut hi = n - 1;
        while lo < hi {
            let mid = (lo + hi) >> 1;
            if cum[mid] < d {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        let seg_start = cum[lo - 1];
        let seg_len = cum[lo] - seg_start;
        let lt = if seg_len > 0.01 {
            (d - seg_start) / seg_len
        } else {
            0.0
        };
        let ax = self.pts_x[off + lo - 1];
        let ay = self.pts_y[off + lo - 1];
        let bx = self.pts_x[off + lo];
        let by = self.pts_y[off + lo];
        (ax + (bx - ax) * lt, ay + (by - ay) * lt)
    }

    /// Position and perpendicular unit vector at t along an edge's polyline.
    /// The perpendicular is computed from the local polyline segment tangent,
    /// using (-dy, dx)/len which is the true geometric perpendicular.
    #[inline(always)]
    pub fn edge_pos_perp(&self, ei: u32, t: f32) -> (f32, f32, f32, f32) {
        let t = t.clamp(0.0, 1.0);
        let total = self.edge_total_len[ei as usize];
        let d = t * total;

        let off = self.pts_offset[ei as usize] as usize;
        let end = self.pts_offset[ei as usize + 1] as usize;
        let n = end - off;
        if n < 2 {
            return (self.pts_x[off], self.pts_y[off], 0.0, 0.0);
        }

        let cum = &self.cum_dist[off..end];
        let mut lo = 1usize;
        let mut hi = n - 1;
        while lo < hi {
            let mid = (lo + hi) >> 1;
            if cum[mid] < d {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        let seg_start = cum[lo - 1];
        let seg_len = cum[lo] - seg_start;
        let lt = if seg_len > 0.01 {
            (d - seg_start) / seg_len
        } else {
            0.0
        };
        let ax = self.pts_x[off + lo - 1];
        let ay = self.pts_y[off + lo - 1];
        let bx = self.pts_x[off + lo];
        let by = self.pts_y[off + lo];

        let x = ax + (bx - ax) * lt;
        let y = ay + (by - ay) * lt;

        // Perpendicular = (-dy, dx) / segment_length
        let dx = bx - ax;
        let dy = by - ay;
        let inv_len = 1.0 / seg_len.max(0.001);
        let perp_x = -dy * inv_len;
        let perp_y = dx * inv_len;

        (x, y, perp_x, perp_y)
    }

    /// Bearing at edge endpoint. `at_end=true` = arrival bearing, `false` = departure bearing.
    #[inline]
    pub fn edge_bearing(&self, ei: u32, dir: i8, at_end: bool) -> f32 {
        let off = self.pts_offset[ei as usize] as usize;
        let end = self.pts_offset[ei as usize + 1] as usize;
        let n = end - off;
        if n < 2 {
            return 0.0;
        }
        let (ax, ay, bx, by) = if at_end {
            if dir > 0 {
                // Arriving at "to" end
                let a = if n >= 2 { off + n - 2 } else { off };
                let b = off + n - 1;
                (self.pts_x[a], self.pts_y[a], self.pts_x[b], self.pts_y[b])
            } else {
                // Arriving at "from" end (reverse)
                let a = if n >= 2 { off + 1 } else { off + n - 1 };
                let b = off;
                (self.pts_x[a], self.pts_y[a], self.pts_x[b], self.pts_y[b])
            }
        } else {
            // Departure bearing
            if dir > 0 {
                let a = off;
                let b = if n >= 2 { off + 1 } else { off + n - 1 };
                (self.pts_x[a], self.pts_y[a], self.pts_x[b], self.pts_y[b])
            } else {
                let a = off + n - 1;
                let b = if n >= 2 { off + n - 2 } else { off };
                (self.pts_x[a], self.pts_y[a], self.pts_x[b], self.pts_y[b])
            }
        };
        (bx - ax).atan2(by - ay)
    }

    /// Get edges for a node (slice into node_edges_data)
    #[inline]
    pub fn node_edges(&self, node_idx: u32) -> &[u32] {
        let start = self.node_edges_offset[node_idx as usize] as usize;
        let end = self.node_edges_offset[node_idx as usize + 1] as usize;
        &self.node_edges_data[start..end]
    }

    /// Select a random edge weighted by spawn probability (binary search on CDF).
    #[inline]
    pub fn spawn_edge(&self, r: f32) -> u32 {
        let r = r.clamp(0.0, 0.99999);
        let cdf = &self.edge_spawn_cdf;
        let mut lo = 0usize;
        let mut hi = cdf.len();
        while lo < hi {
            let mid = (lo + hi) >> 1;
            if cdf[mid] < r {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        lo.min(self.edge_count as usize - 1) as u32
    }

    /// Check if a node has turn ratio data.
    #[inline]
    pub fn has_turns(&self, node: u32) -> bool {
        self.node_volume[node as usize] > 0.0
    }

    /// Get turn ratios for a node + approach index (0=EB, 1=WB, 2=NB, 3=SB).
    /// Returns (left_ratio, thru_ratio, right_ratio).
    #[inline]
    pub fn turn_ratios(&self, node: u32, approach: u32) -> (f32, f32, f32) {
        let base = node as usize * 12 + approach as usize * 3;
        (
            self.node_turns[base],
            self.node_turns[base + 1],
            self.node_turns[base + 2],
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_test_graph() -> Graph {
        let json = r#"{
            "w": 100, "h": 100,
            "nodes": [
                {"id": 0, "x": 0.0, "y": 0.0, "lat": 0.0, "lon": 0.0, "light": false, "edges": [0]},
                {"id": 1, "x": 100.0, "y": 0.0, "lat": 0.0, "lon": 0.0, "light": false, "edges": [0]}
            ],
            "edges": [
                {"from": 0, "to": 1, "pts": [[0.0, 0.0], [50.0, 0.0], [100.0, 0.0]], "speed": 50.0, "oneway": false, "congestion": [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}
            ],
            "routes": []
        }"#;
        Graph::from_json(json).unwrap()
    }

    #[test]
    fn edge_pos_endpoints() {
        let g = make_test_graph();
        let (x0, y0) = g.edge_pos(0, 0.0);
        assert!((x0 - 0.0).abs() < 0.1);
        assert!((y0 - 0.0).abs() < 0.1);
        let (x1, y1) = g.edge_pos(0, 1.0);
        assert!((x1 - 100.0).abs() < 0.1);
        assert!((y1 - 0.0).abs() < 0.1);
    }

    #[test]
    fn edge_pos_midpoint() {
        let g = make_test_graph();
        let (x, y) = g.edge_pos(0, 0.5);
        assert!((x - 50.0).abs() < 0.1);
        assert!((y - 0.0).abs() < 0.1);
    }

    #[test]
    fn total_len_correct() {
        let g = make_test_graph();
        assert!((g.edge_total_len[0] - 100.0).abs() < 0.1);
    }

    #[test]
    fn turns_vol_deserialization() {
        let json = r#"{
            "w": 100, "h": 100,
            "nodes": [
                {"id": 0, "x": 0.0, "y": 0.0, "lat": 34.0, "lon": -118.0, "light": true, "edges": [0],
                 "signal": [60.0, 38.3, 60.0, 33.6, 60.0, 38.3, 60.0, 30.0],
                 "turns": [0.0, 0.0, 0.0, 0.9973, 0.0, 0.0027, 0.0, 0.6096, 0.3904, 0.0, 1.0, 0.0],
                 "vol": 2843},
                {"id": 1, "x": 100.0, "y": 0.0, "lat": 34.0, "lon": -118.0, "light": false, "edges": [0]}
            ],
            "edges": [
                {"from": 0, "to": 1, "pts": [[0.0, 0.0], [100.0, 0.0]], "speed": 50.0, "oneway": false, "congestion": [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}
            ],
            "hourly_volume": [0.27, 0.13, 0.19, 0.27, 0.52, 0.55, 0.45, 0.79, 0.83, 0.68, 0.30, 0.34, 0.36, 0.30, 0.31, 0.80, 0.88, 1.0, 0.46, 0.15, 0.15, 0.17, 0.13, 0.23],
            "total_counted_volume": 13020656
        }"#;
        let g = Graph::from_json(json).unwrap();
        assert_eq!(g.counted_nodes.len(), 1, "should have 1 counted node");
        assert_eq!(g.counted_nodes[0], 0);
        assert!(
            (g.node_volume[0] - 2843.0).abs() < 1.0,
            "vol should be 2843"
        );
        assert!(g.node_turns[3] > 0.99, "WB_L should be ~0.9973");
        assert!(
            (g.hourly_volume[17] - 1.0).abs() < 0.01,
            "peak hour should be 1.0"
        );
        assert!(g.total_counted_volume > 13_000_000.0);
    }

    #[test]
    fn stop_and_lanes_deserialization() {
        let json = r#"{
            "w": 100, "h": 100,
            "nodes": [
                {"id": 0, "x": 0.0, "y": 0.0, "lat": 34.0, "lon": -118.0, "light": false, "stop": true, "edges": [0]},
                {"id": 1, "x": 100.0, "y": 0.0, "lat": 34.0, "lon": -118.0, "light": true, "edges": [0]},
                {"id": 2, "x": 200.0, "y": 0.0, "lat": 34.0, "lon": -118.0, "light": false, "edges": [1]}
            ],
            "edges": [
                {"from": 0, "to": 1, "pts": [[0.0, 0.0], [100.0, 0.0]], "speed": 50.0, "oneway": false, "congestion": [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0], "lanes": 3},
                {"from": 1, "to": 2, "pts": [[100.0, 0.0], [200.0, 0.0]], "speed": 25.0, "oneway": false, "congestion": [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}
            ]
        }"#;
        let g = Graph::from_json(json).unwrap();
        // Node 0: stop sign
        assert!(g.node_stop[0], "node 0 should be stop");
        assert!(!g.node_light[0], "node 0 should not be light");
        // Node 1: traffic signal
        assert!(!g.node_stop[1], "node 1 should not be stop");
        assert!(g.node_light[1], "node 1 should be light");
        // Node 2: neither (stop defaults to false)
        assert!(!g.node_stop[2], "node 2 should not be stop");
        // Edge 0: explicit 3 lanes
        assert_eq!(g.edge_lanes[0], 3, "edge 0 should have 3 lanes");
        // Edge 1: missing lanes field → default 1
        assert_eq!(g.edge_lanes[1], 1, "edge 1 should default to 1 lane");
    }

    #[test]
    fn backward_compat_no_stop_no_lanes() {
        // Verify old graph.json format (no stop/lanes) still parses
        let json = r#"{
            "w": 100, "h": 100,
            "nodes": [
                {"id": 0, "x": 0.0, "y": 0.0, "lat": 0.0, "lon": 0.0, "light": true, "edges": [0]},
                {"id": 1, "x": 100.0, "y": 0.0, "lat": 0.0, "lon": 0.0, "light": false, "edges": [0]}
            ],
            "edges": [
                {"from": 0, "to": 1, "pts": [[0.0, 0.0], [100.0, 0.0]], "speed": 50.0, "oneway": false, "congestion": [0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]}
            ]
        }"#;
        let g = Graph::from_json(json).unwrap();
        assert!(!g.node_stop[0], "stop defaults to false");
        assert!(!g.node_stop[1], "stop defaults to false");
        assert_eq!(g.edge_lanes[0], 1, "lanes defaults to 1");
    }
}
