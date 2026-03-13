use crate::graph::{Graph, METERS_PER_PIXEL};
use crate::math::Rng;

const MAX_CARS: usize = 65536;
const CONGESTION_FACTOR: [f32; 4] = [0.95, 0.70, 0.45, 0.20];

/// Number of position history entries per car for trail rendering.
pub const TRAIL_LEN: usize = 32;

/// Minimum physics timestep in sim-seconds. Below this, ticks are combined.
const MIN_PHYSICS_DT: f32 = 0.5;
/// Maximum physics ticks per rendered frame (caps CPU cost at high day_speed).
const MAX_TICKS_PER_FRAME: u32 = 60;

pub struct ParticleState {
    // Position
    pub car_edge: Vec<u32>,
    pub car_dir: Vec<i8>,
    pub car_t: Vec<f32>,
    pub car_accel: Vec<f32>,        // [0, 1] smooth speed multiplier
    pub car_green_wait: Vec<u8>,    // frames until allowed to move (queue discharge stagger)
    pub car_ttl: Vec<u8>,           // edges remaining until despawn

    // Per-edge queue tracking (reset each tick)
    pub edge_queue_tail_fwd: Vec<f32>, // rearmost stopped car t-position (1.1 = no queue)
    pub edge_queue_tail_rev: Vec<f32>, // rearmost stopped car t-position (-0.1 = no queue)

    // Per-edge car count (reset each tick, used for density speed limit)
    pub edge_car_count: Vec<u16>,

    // Car-following: lead car t-position per car (rebuilt each physics tick, NaN = no car ahead)
    pub car_lead_t: Vec<f32>,
    // Working buffer for car-following sort (sort_key, car_idx)
    follow_sort: Vec<(u64, u32)>,

    // Computed each tick — head and tail positions for rendering
    pub car_x: Vec<f32>,
    pub car_y: Vec<f32>,
    pub car_tx: Vec<f32>,  // 5m body tail
    pub car_ty: Vec<f32>,

    // Per-car lateral jitter: random offset in [-1, 1], applied as render-only perpendicular shift
    pub car_jitter: Vec<f32>,

    // Trail history ring buffer: records (x,y) each frame for path tracing
    pub car_trail_x: Vec<f32>,   // flat [MAX_CARS * TRAIL_LEN]
    pub car_trail_y: Vec<f32>,   // flat [MAX_CARS * TRAIL_LEN]
    pub car_trail_head: Vec<u8>, // write index per car (0..TRAIL_LEN)
    pub car_trail_len: Vec<u8>,  // valid entries per car (0..TRAIL_LEN)

    // Parking simulation
    pub car_seeking: Vec<bool>,        // is this car a parking seeker?
    pub parking_occupancy: Vec<u32>,   // current occupancy per node (indexed by node index)
    pub parked_total: u32,             // total cars currently parked
    pub seeking_count: u32,            // current seekers in traffic

    // Park flash: brief glow when a car parks (ring buffer, max 256 active)
    pub parked_x: Vec<f32>,            // positions of parked cars (persistent)
    pub parked_y: Vec<f32>,
    pub parked_node: Vec<u32>,          // which node each parked car is at (for departure removal)

    pub car_count: u32,
    pub follow_idx: Option<u32>,
    pub rng: Rng,

    /// Debug: number of transfer_car calls that took the gravity path this frame
    pub gravity_transfers: u32,
}

pub struct SimParams {
    pub density: f32,
    pub day_speed: f32,
    pub trail_length_m: f32,
    pub jitter: f32, // max lateral offset in pixels (0-5)
}

pub struct SimClock {
    pub sim_time: f32,
    pub current_hour: u32,
    pub next_hour: u32,
    pub hour_blend: f32,
}

impl ParticleState {
    pub fn new() -> Self {
        Self {
            car_edge: vec![0; MAX_CARS],
            car_dir: vec![0; MAX_CARS],
            car_t: vec![0.0; MAX_CARS],
            car_accel: vec![1.0; MAX_CARS],
            car_green_wait: vec![0; MAX_CARS],
            car_ttl: vec![0; MAX_CARS],
            edge_queue_tail_fwd: Vec::new(),
            edge_queue_tail_rev: Vec::new(),
            edge_car_count: Vec::new(),
            car_lead_t: vec![f32::NAN; MAX_CARS],
            follow_sort: Vec::new(),
            car_x: vec![0.0; MAX_CARS],
            car_y: vec![0.0; MAX_CARS],
            car_tx: vec![0.0; MAX_CARS],
            car_ty: vec![0.0; MAX_CARS],
            car_jitter: vec![0.0; MAX_CARS],
            car_trail_x: vec![0.0; MAX_CARS * TRAIL_LEN],
            car_trail_y: vec![0.0; MAX_CARS * TRAIL_LEN],
            car_trail_head: vec![0; MAX_CARS],
            car_trail_len: vec![0; MAX_CARS],
            car_seeking: vec![false; MAX_CARS],
            parking_occupancy: Vec::new(), // sized on first use
            parked_total: 0,
            seeking_count: 0,
            parked_x: Vec::new(),
            parked_y: Vec::new(),
            parked_node: Vec::new(),
            car_count: 0,
            follow_idx: None,
            rng: Rng::new(12345),
            gravity_transfers: 0,
        }
    }

    pub fn spawn_initial(&mut self, graph: &Graph, params: &SimParams) {
        self.car_count = 0;

        // Initialize per-edge queue tracking
        let ec = graph.edge_count as usize;
        self.edge_queue_tail_fwd.resize(ec, 1.1);
        self.edge_queue_tail_rev.resize(ec, -0.1);
        self.edge_car_count.resize(ec, 0);

        // Initialize parking occupancy
        self.parking_occupancy.resize(graph.node_count as usize, 0);
        self.parked_total = 0;
        self.seeking_count = 0;

        // Distribute ambient cars across edges by spawn weight
        let density = params.density / 100.0;
        let density_mult = 0.003;
        for i in 0..ec {
            if self.car_count as usize >= MAX_CARS {
                break;
            }
            let total_len = graph.edge_total_len[i];
            let expected = total_len * density_mult * density;
            let n = expected as u32
                + if self.rng.next_f32() < expected.fract() {
                    1
                } else {
                    0
                };
            for _ in 0..n {
                if self.car_count as usize >= MAX_CARS {
                    break;
                }
                let dir = if !graph.edge_oneway[i] && self.rng.next_f32() < 0.5 {
                    -1i8
                } else {
                    1
                };
                let c = self.car_count as usize;
                self.car_edge[c] = i as u32;
                self.car_dir[c] = dir;
                self.car_t[c] = self.rng.next_f32();
                self.car_accel[c] = 1.0;
                self.car_green_wait[c] = 0;
                self.car_ttl[c] = 80 + (self.rng.next_range(176) as u8);
                self.car_seeking[c] = false;
                self.car_count += 1;
            }
        }
    }

    pub fn tick(&mut self, graph: &Graph, params: &SimParams, clock: &mut SimClock, gravity_x: f32, gravity_y: f32, gravity_active: bool, gravity_strength: f32) {
        self.gravity_transfers = 0;
        if params.day_speed > 0.0 {
            // day_speed controls how many sim-seconds to advance per frame.
            // At default slider=20 (internal 0.2): 0.6 sim-sec/frame → ~1 sim-min/real-sec.
            // A car at 50km/h covers ~2px/frame — visually smooth, not too fast.
            let sim_advance_seconds = params.day_speed * 3.0;
            let n_ticks = ((sim_advance_seconds / MIN_PHYSICS_DT).ceil() as u32)
                .max(1)
                .min(MAX_TICKS_PER_FRAME);
            // Actual sim-seconds per tick (may exceed MIN_PHYSICS_DT when capped)
            let dt_per_tick = sim_advance_seconds / n_ticks as f32;
            let clock_step_hours = dt_per_tick / 3600.0;

            for _ in 0..n_ticks {
                // Advance clock
                clock.sim_time += clock_step_hours;
                if clock.sim_time >= 24.0 {
                    clock.sim_time -= 24.0;
                }
                clock.current_hour = (clock.sim_time.floor() as u32) % 24;
                clock.next_hour = (clock.current_hour + 1) % 24;
                clock.hour_blend = clock.sim_time % 1.0;

                self.physics_step(graph, params, clock, dt_per_tick, gravity_x, gravity_y, gravity_active, gravity_strength);
            }
        }

        // Parking departures: ~0.1% chance per occupied space per tick
        for &ni in &graph.parking_nodes {
            let occ = self.parking_occupancy[ni as usize];
            if occ > 0 {
                let mut departures = 0u32;
                for _ in 0..occ {
                    if self.rng.next_f32() < 0.001 {
                        departures += 1;
                    }
                }
                if departures > 0 {
                    let departures = departures.min(occ);
                    self.parking_occupancy[ni as usize] -= departures;
                    self.parked_total = self.parked_total.saturating_sub(departures);
                    // Remove parked car dots for this node (swap-remove from the back)
                    let mut removed = 0u32;
                    let mut i = 0;
                    while i < self.parked_x.len() && removed < departures {
                        if self.parked_node[i] == ni {
                            let last = self.parked_x.len() - 1;
                            self.parked_x.swap(i, last);
                            self.parked_y.swap(i, last);
                            self.parked_node.swap(i, last);
                            self.parked_x.pop();
                            self.parked_y.pop();
                            self.parked_node.pop();
                            removed += 1;
                        } else {
                            i += 1;
                        }
                    }
                }
            }
        }

        // Maintain population + render positions (always, even when paused)
        self.maintain_population(graph, params, clock, gravity_active);

        self.compute_positions(graph, params.trail_length_m, params.jitter);
    }

    /// One physics tick: move cars, handle signals, queue.
    /// dt_seconds is the sim-seconds this tick represents.
    fn physics_step(&mut self, graph: &Graph, _params: &SimParams, clock: &SimClock, dt_seconds: f32, gravity_x: f32, gravity_y: f32, gravity_active: bool, gravity_strength: f32) {
        let period_off = signal_period_offset(clock.current_hour);

        // Reset per-edge queue tracking
        for v in self.edge_queue_tail_fwd.iter_mut() {
            *v = 1.1;
        }
        for v in self.edge_queue_tail_rev.iter_mut() {
            *v = -0.1;
        }

        // Physical gap between queued cars (adjusted by lane count)
        const CAR_GAP_M: f32 = 8.0;
        let base_gap_px = CAR_GAP_M / METERS_PER_PIXEL;

        // Pre-pass: count cars per edge for density-based speed limits
        for v in self.edge_car_count.iter_mut() {
            *v = 0;
        }
        let n = self.car_count as usize;
        for c in 0..n {
            let ei = self.car_edge[c] as usize;
            if ei < self.edge_car_count.len() {
                self.edge_car_count[ei] = self.edge_car_count[ei].saturating_add(1);
            }
        }

        // Car-following pre-pass: sort all cars by (edge, direction, position)
        // then link consecutive same-edge-direction cars as leader/follower pairs.
        // This is order-independent — uses start-of-tick positions.
        self.follow_sort.clear();
        for c in 0..n {
            let ei = self.car_edge[c] as u64;
            let dir_bit = if self.car_dir[c] > 0 { 0u64 } else { 1u64 };
            let t = self.car_t[c];
            // Sort key: edge in upper bits, dir in bit 32, t-bits in lower 32.
            // For fwd: ascending t (behind→ahead). For rev: descending t (behind→ahead).
            let t_bits = if dir_bit == 0 { t.to_bits() as u64 } else { !(t.to_bits()) as u64 };
            let key = (ei << 33) | (dir_bit << 32) | t_bits;
            self.follow_sort.push((key, c as u32));
            self.car_lead_t[c] = f32::NAN;
        }
        self.follow_sort.sort_unstable_by_key(|&(k, _)| k);

        // Walk sorted list: link consecutive same-edge-direction pairs
        for w in 1..self.follow_sort.len() {
            let (key_behind, idx_behind) = self.follow_sort[w - 1];
            let (key_ahead, idx_ahead) = self.follow_sort[w];
            if (key_behind >> 32) == (key_ahead >> 32) {
                // Same edge + direction: behind follows ahead
                self.car_lead_t[idx_behind as usize] = self.car_t[idx_ahead as usize];
            }
        }

        // Update all cars
        let mut i = 0u32;
        while (i as usize) < self.car_count as usize {
            let idx = i as usize;
            let ei = self.car_edge[idx] as usize;

            if ei >= graph.edge_count as usize {
                self.recycle_car(idx, graph);
                i += 1;
                continue;
            }

            // Queue discharge stagger
            if self.car_green_wait[idx] > 0 {
                self.car_green_wait[idx] -= 1;
                let dir = self.car_dir[idx];
                let car_t = self.car_t[idx];
                let total_len = graph.edge_total_len[ei].max(1.0);
                let lanes = graph.edge_lanes[ei].max(1) as f32;
                let gap_t = (base_gap_px / lanes) / total_len;
                if dir > 0 {
                    self.edge_queue_tail_fwd[ei] =
                        self.edge_queue_tail_fwd[ei].min(car_t - gap_t);
                } else {
                    self.edge_queue_tail_rev[ei] =
                        self.edge_queue_tail_rev[ei].max(car_t + gap_t);
                }
                i += 1;
                continue;
            }

            let dir = self.car_dir[idx];
            let car_t = self.car_t[idx];
            let total_len = graph.edge_total_len[ei].max(1.0);
            let lanes = graph.edge_lanes[ei].max(1) as f32;
            let gap_t = (base_gap_px / lanes) / total_len;

            // Movement for this tick
            let mut base_dt = edge_dt(graph, ei, clock, dt_seconds);

            // Density-based speed limit: capacity based on realistic following distance,
            // not bumper-to-bumper gap. Uses 1.5-second rule at the edge's speed limit.
            let count = self.edge_car_count[ei] as f32;
            let speed_ms = graph.edge_speed[ei] * (1000.0 / 3600.0);
            let follow_m = (speed_ms * 1.5).max(10.0);
            let capacity = (total_len * METERS_PER_PIXEL * lanes) / follow_m;
            let occupancy = count / capacity.max(1.0);
            if occupancy > 0.3 {
                let density_factor = ((1.0 - (occupancy - 0.3) / 0.7) as f32).max(0.15);
                base_dt *= density_factor;
            }

            // Gravity speed penalty: cars near cursor slow, creating visible congestion.
            // Uses approximate car position (linear interp between edge endpoints).
            if gravity_active && gravity_strength > 0.0 {
                let from = graph.edge_from[ei] as usize;
                let to = graph.edge_to[ei] as usize;
                let cx = graph.node_x[from] + (graph.node_x[to] - graph.node_x[from]) * car_t;
                let cy = graph.node_y[from] + (graph.node_y[to] - graph.node_y[from]) * car_t;
                let dx = gravity_x - cx;
                let dy = gravity_y - cy;
                let dist = (dx * dx + dy * dy).sqrt();
                let grav_slow = (1.0 - dist / 600.0).max(0.0);
                let grav_factor = (1.0 - grav_slow * grav_slow * (gravity_strength / 100.0) * 0.7).max(0.10);
                base_dt *= grav_factor;
            }

            let mut stop = find_stop_point(
                ei,
                dir,
                car_t,
                gap_t,
                graph,
                clock,
                period_off,
                &self.edge_queue_tail_fwd,
                &self.edge_queue_tail_rev,
            );

            // Car-following: speed-dependent following distance (1.5-second rule).
            // NOT divided by lanes — in this 1D model, lane parallelism is handled
            // by the density factor, not by reducing individual follow distance.
            let lead = self.car_lead_t[idx];
            if !lead.is_nan() {
                let speed_t_per_s = base_dt / dt_seconds;
                let follow_time = 1.5; // seconds of headway
                let min_follow_m = 10.0; // minimum gap even when nearly stopped
                let min_follow_t = min_follow_m / (METERS_PER_PIXEL * total_len);
                let follow_gap = (speed_t_per_s * self.car_accel[idx] * follow_time).max(min_follow_t);
                let follow_stop = if dir > 0 {
                    (lead - follow_gap).max(0.0)
                } else {
                    (lead + follow_gap).min(1.0)
                };
                let is_ahead = if dir > 0 { follow_stop > car_t + 0.001 } else { follow_stop < car_t - 0.001 };
                if is_ahead {
                    stop = Some(match stop {
                        Some(s) if dir > 0 => s.min(follow_stop),
                        Some(s) => s.max(follow_stop),
                        None => follow_stop,
                    });
                }
            }

            // ── Deceleration / acceleration ──
            let target_accel = if let Some(stop_at) = stop {
                let dist = if dir > 0 {
                    stop_at - car_t
                } else {
                    car_t - stop_at
                };
                if dist <= 0.003 {
                    0.0
                } else {
                    let decel_zone = 0.20;
                    if dist < decel_zone {
                        let x = dist / decel_zone;
                        (x * x * (3.0 - 2.0 * x)).max(0.005)
                    } else {
                        1.0
                    }
                }
            } else {
                1.0
            };

            // Register in queue: any car approaching a stop point reserves its slot.
            // This prevents multiple cars from targeting the same stop position.
            if let Some(_) = stop {
                if target_accel < 1.0 {
                    if dir > 0 {
                        self.edge_queue_tail_fwd[ei] =
                            self.edge_queue_tail_fwd[ei].min(car_t - gap_t);
                    } else {
                        self.edge_queue_tail_rev[ei] =
                            self.edge_queue_tail_rev[ei].max(car_t + gap_t);
                    }
                }
            }

            // Smooth accel: instant decel (braking), gradual dt-dependent accel.
            // Tau varies with current speed: very slow from stop (reaction time +
            // gear engagement), faster once rolling. Creates visible wave dissolution
            // where each car takes real wall-clock time to get going.
            if target_accel < self.car_accel[idx] {
                self.car_accel[idx] = target_accel;
            } else {
                let current = self.car_accel[idx];
                let tau = 2.0 + 8.0 * (1.0 - current); // 10s from stop, 2s at cruise
                let factor = (dt_seconds / tau).min(0.25);
                self.car_accel[idx] += (target_accel - self.car_accel[idx]) * factor;
            }

            let mut dt = base_dt * self.car_accel[idx];

            // Clamp so we don't overshoot a stop point
            if let Some(stop_at) = stop {
                let max_dist = if dir > 0 {
                    stop_at - car_t
                } else {
                    car_t - stop_at
                };
                if max_dist > 0.0 {
                    dt = dt.min(max_dist * 0.9);
                }
            }

            // Skip movement if effectively stopped
            if dt < 0.0001 && target_accel == 0.0 {
                i += 1;
                continue;
            }

            self.car_t[idx] = car_t + self.car_dir[idx] as f32 * dt;

            // ── Edge transitions (loop for multi-edge crossing at high day_speed) ──
            let mut hops = 0u32;
            loop {
                let cur_dir = self.car_dir[idx];
                let cur_t = self.car_t[idx];
                let cur_ei = self.car_edge[idx] as usize;

                if cur_t >= 1.0 && cur_dir > 0 {
                    let overshoot_frac = cur_t - 1.0;
                    let node = graph.edge_to[cur_ei];
                    self.car_accel[idx] = 0.5;
                    if !self.transfer_car(idx, node, graph, gravity_x, gravity_y, gravity_active, gravity_strength) {
                        break; // despawned
                    }
                    hops += 1;
                    if hops >= 4 { break; }
                    // Convert overshoot from old edge to new edge (by pixel length ratio)
                    let old_len = graph.edge_total_len[cur_ei].max(1.0);
                    let new_ei = self.car_edge[idx] as usize;
                    let new_len = graph.edge_total_len[new_ei].max(1.0);
                    let carry = overshoot_frac * old_len / new_len;
                    let new_dir = self.car_dir[idx];
                    self.car_t[idx] = if new_dir > 0 {
                        0.001 + carry
                    } else {
                        0.999 - carry
                    };
                } else if cur_t <= 0.0 && cur_dir < 0 {
                    let overshoot_frac = -cur_t;
                    let node = graph.edge_from[cur_ei];
                    self.car_accel[idx] = 0.5;
                    if !self.transfer_car(idx, node, graph, gravity_x, gravity_y, gravity_active, gravity_strength) {
                        break; // despawned
                    }
                    hops += 1;
                    if hops >= 4 { break; }
                    let old_len = graph.edge_total_len[cur_ei].max(1.0);
                    let new_ei = self.car_edge[idx] as usize;
                    let new_len = graph.edge_total_len[new_ei].max(1.0);
                    let carry = overshoot_frac * old_len / new_len;
                    let new_dir = self.car_dir[idx];
                    self.car_t[idx] = if new_dir > 0 {
                        0.001 + carry
                    } else {
                        0.999 - carry
                    };
                } else {
                    break; // no overshoot, done
                }
            }
            i += 1;
        }
    }

    /// Compute head (x,y) and tail (tx,ty) for all live cars.
    /// Also records head position into the trail ring buffer.
    fn compute_positions(&mut self, graph: &Graph, _trail_length_m: f32, jitter_px: f32) {
        const CAR_LENGTH_M: f32 = 5.0;

        for idx in 0..self.car_count as usize {
            let ei = self.car_edge[idx];
            if ei >= graph.edge_count {
                continue;
            }
            let t = self.car_t[idx];
            let dir = self.car_dir[idx];
            let offset = self.car_jitter[idx] * jitter_px;

            // Head position with local perpendicular offset
            let (x, y, px, py) = graph.edge_pos_perp(ei, t);
            let hx = x + px * offset;
            let hy = y + py * offset;
            self.car_x[idx] = hx;
            self.car_y[idx] = hy;

            // Body tail: base 5m + speed-proportional stretch (motion blur).
            // Fast highway cars get longer streaks → more bloom geometry.
            let speed = graph.edge_speed[ei as usize];
            let accel = self.car_accel[idx];
            let body_len_px = (CAR_LENGTH_M + speed * accel * 0.15) / METERS_PER_PIXEL;
            let total_len = graph.edge_total_len[ei as usize].max(1.0);
            let trail_dt = body_len_px / total_len;
            let tail_t = (t - dir as f32 * trail_dt).clamp(0.0, 1.0);
            let (tx, ty, tpx, tpy) = graph.edge_pos_perp(ei, tail_t);
            self.car_tx[idx] = tx + tpx * offset;
            self.car_ty[idx] = ty + tpy * offset;

            // Record head position into trail ring buffer
            let base = idx * TRAIL_LEN;
            let h = self.car_trail_head[idx] as usize;
            self.car_trail_x[base + h] = hx;
            self.car_trail_y[base + h] = hy;
            self.car_trail_head[idx] = ((h + 1) % TRAIL_LEN) as u8;
            if (self.car_trail_len[idx] as usize) < TRAIL_LEN {
                self.car_trail_len[idx] += 1;
            }
        }
    }

    /// Select next edge at intersection using turn ratios.
    /// Returns false if car was despawned (dead end or TTL expired).
    fn transfer_car(&mut self, idx: usize, node_idx: u32, graph: &Graph, gravity_x: f32, gravity_y: f32, gravity_active: bool, gravity_strength: f32) -> bool {
        // Decrement TTL
        if self.car_ttl[idx] == 0 {
            self.despawn_car(idx);
            return false;
        }
        self.car_ttl[idx] -= 1;

        if node_idx >= graph.node_count {
            self.despawn_car(idx);
            return false;
        }

        // Parking attempt: seeker at a node with available parking near gravity
        if self.car_seeking[idx] && graph.node_parking[node_idx as usize] > 0 {
            let nx = graph.node_x[node_idx as usize];
            let ny = graph.node_y[node_idx as usize];
            let dx = gravity_x - nx;
            let dy = gravity_y - ny;
            let dist = (dx * dx + dy * dy).sqrt();
            if dist < 200.0 && self.parking_occupancy[node_idx as usize] < graph.node_parking[node_idx as usize] {
                // Park successfully — add persistent parked car dot with jitter
                self.parking_occupancy[node_idx as usize] += 1;
                self.parked_total += 1;
                let jx = (self.rng.next_f32() - 0.5) * 8.0;
                let jy = (self.rng.next_f32() - 0.5) * 8.0;
                self.parked_x.push(nx + jx);
                self.parked_y.push(ny + jy);
                self.parked_node.push(node_idx);
                // despawn_car will decrement seeking_count
                self.despawn_car(idx);
                return false;
            }
        }

        let from_edge = self.car_edge[idx];
        let from_dir = self.car_dir[idx];

        // Get arrival bearing from precomputed values
        let arrival_bearing = if from_dir > 0 {
            graph.edge_bearing_fwd[from_edge as usize]
        } else {
            graph.edge_bearing_rev[from_edge as usize]
        };

        let node_edges = graph.node_edges(node_idx);

        // Collect valid exits with their departure bearings
        let mut exit_ei = [0u32; 16];
        let mut exit_dir = [0i8; 16];
        let mut exit_bearing = [0.0f32; 16];
        let mut n_exits = 0usize;

        for &ei in node_edges {
            if ei == from_edge || ei >= graph.edge_count {
                continue;
            }
            if n_exits >= 16 {
                break;
            }
            if graph.edge_from[ei as usize] == node_idx {
                exit_ei[n_exits] = ei;
                exit_dir[n_exits] = 1;
                exit_bearing[n_exits] = graph.edge_bearing(ei, 1, false);
                n_exits += 1;
            }
            if graph.edge_to[ei as usize] == node_idx && !graph.edge_oneway[ei as usize] {
                if n_exits >= 16 {
                    break;
                }
                exit_ei[n_exits] = ei;
                exit_dir[n_exits] = -1;
                exit_bearing[n_exits] = graph.edge_bearing(ei, -1, false);
                n_exits += 1;
            }
        }

        if n_exits == 0 {
            // Dead end
            self.despawn_car(idx);
            return false;
        }

        // Classify arrival bearing into approach: 0=EB, 1=WB, 2=NB, 3=SB
        let approach = bearing_to_approach(arrival_bearing);

        // Get turn ratios for this node + approach
        #[allow(unused_variables)]
        let (p_left, p_thru, p_right) = if graph.has_turns(node_idx) {
            let (l, t, r) = graph.turn_ratios(node_idx, approach);
            if l + t + r > 0.01 {
                (l, t, r)
            } else {
                (0.10, 0.80, 0.10) // approach has no data
            }
        } else {
            (0.10, 0.80, 0.10) // uncounted node default
        };

        // Classify each exit's turn angle
        let mut exit_turn_angle = [0.0f32; 16];
        for j in 0..n_exits {
            let depart = exit_bearing[j];
            let mut turn_angle = depart - arrival_bearing;
            while turn_angle > std::f32::consts::PI {
                turn_angle -= 2.0 * std::f32::consts::PI;
            }
            while turn_angle < -std::f32::consts::PI {
                turn_angle += 2.0 * std::f32::consts::PI;
            }
            exit_turn_angle[j] = turn_angle;
        }

        // When gravity is active, use weighted random selection blending
        // turn ratios with gravity proximity so cars actually reroute.
        // Without gravity, use the original deterministic best-match logic.
        let chosen = if gravity_active && gravity_strength > 0.0 {
            self.gravity_transfers += 1;

            // Gravity well routing: blend turn ratios with proximity to cursor.
            // strength 0-30: gentle bias, turn ratios still dominate
            // strength 30-70: strong pull, cars noticeably reroute
            // strength 70-100: full Moses mode, pure greedy toward cursor
            let falloff = 1500.0; // radius of influence in graph px (~half the map)
            // power scales quadratically: 0→0, 50→25, 100→100
            let base_power = gravity_strength * gravity_strength / 100.0;
            // Seekers get 2x gravity pull to converge on the event
            let power = if self.car_seeking[idx] { base_power * 2.0 } else { base_power };

            let mut weights = [0.0f32; 16];
            let mut total_weight = 0.0f32;

            for j in 0..n_exits {
                let ta = exit_turn_angle[j];

                // Base weight from turn-type classification
                let is_uturn = (ta.abs() - std::f32::consts::PI).abs() < std::f32::consts::FRAC_PI_4;
                let turn_prob = if is_uturn {
                    0.01
                } else if ta.abs() < std::f32::consts::FRAC_PI_4 {
                    p_thru
                } else if ta < 0.0 {
                    p_left
                } else {
                    p_right
                };

                // Gravity proximity: how close this exit leads to cursor
                let dest = if exit_dir[j] > 0 {
                    graph.edge_to[exit_ei[j] as usize]
                } else {
                    graph.edge_from[exit_ei[j] as usize]
                };
                let dx = gravity_x - graph.node_x[dest as usize];
                let dy = gravity_y - graph.node_y[dest as usize];
                let dist = (dx * dx + dy * dy).sqrt();
                let proximity = (1.0 - dist / falloff).max(0.0);
                let gravity_mult = 1.0 + proximity * proximity * power;

                weights[j] = turn_prob * gravity_mult;
                total_weight += weights[j];
            }

            // Weighted random pick
            let mut pick = 0usize;
            if total_weight > 0.0 {
                let mut roll = self.rng.next_f32() * total_weight;
                for j in 0..n_exits {
                    roll -= weights[j];
                    if roll <= 0.0 {
                        pick = j;
                        break;
                    }
                }
            }
            pick
        } else {
            // Original deterministic turn-type matching
            let roll = self.rng.next_f32();
            let turn_type = if roll < p_left {
                TurnType::Left
            } else if roll < p_left + p_thru {
                TurnType::Through
            } else {
                TurnType::Right
            };

            let mut best_idx = 0usize;
            let mut best_score = f32::MAX;
            let mut fallback_idx = 0usize;
            let mut fallback_score = f32::MAX;

            for j in 0..n_exits {
                let turn_angle = exit_turn_angle[j];
                let target = match turn_type {
                    TurnType::Left => -std::f32::consts::FRAC_PI_2,
                    TurnType::Through => 0.0,
                    TurnType::Right => std::f32::consts::FRAC_PI_2,
                };

                let diff = (turn_angle - target).abs();
                if diff < best_score {
                    best_score = diff;
                    best_idx = j;
                }

                let uturn_dist = (turn_angle.abs() - std::f32::consts::PI).abs();
                if uturn_dist > 0.5 && diff < fallback_score {
                    fallback_score = diff;
                    fallback_idx = j;
                }
            }

            if best_score > std::f32::consts::FRAC_PI_2 + 0.3 && fallback_score < f32::MAX {
                fallback_idx
            } else {
                best_idx
            }
        };

        self.car_edge[idx] = exit_ei[chosen];
        self.car_dir[idx] = exit_dir[chosen];
        self.car_t[idx] = if exit_dir[chosen] > 0 {
            0.001
        } else {
            0.999
        };

        // Queue discharge stagger: assign wait based on queue position
        // (will be set by find_stop_point on next tick if signal is red)
        self.car_green_wait[idx] = 0;

        // Stop sign delay: ~3 seconds (6 ticks at ~0.5s/tick)
        if graph.node_stop[node_idx as usize] {
            self.car_green_wait[idx] = 6;
        }

        true
    }

    fn maintain_population(
        &mut self,
        graph: &Graph,
        params: &SimParams,
        clock: &SimClock,
        gravity_active: bool,
    ) {
        let density_frac = params.density / 100.0;

        // Data-driven hourly volume profile from PeMS mainline sensors
        let hv0 = graph.hourly_volume[clock.current_hour as usize];
        let hv1 = graph.hourly_volume[clock.next_hour as usize];
        let volume_scale = hv0 + (hv1 - hv0) * clock.hour_blend;

        // Data-driven peak car count from Little's Law on real intersection volumes,
        // with density slider as a user-controlled multiplier around the data target.
        // peak_cars comes from enrich_graph_turns.py: per-edge flow * travel_time,
        // extrapolated from counted intersections to full network.
        let base_target = if graph.peak_cars > 0.0 {
            graph.peak_cars
        } else {
            // Fallback if no data: use edge count heuristic
            graph.edge_count as f32 * 0.65
        };
        let ambient_target = (base_target * density_frac * volume_scale).floor() as u32;

        // Exclude seekers from population count — they're transient parking traffic,
        // not part of the ambient population. Without this, seekers push car_count
        // above target and get mass-culled before they can reach parking.
        let non_seeking = self.car_count - self.seeking_count.min(self.car_count);

        // Spawn more cars if below target (up to 200 per tick)
        if non_seeking < ambient_target {
            let to_spawn = (ambient_target - non_seeking).min(200);
            for _ in 0..to_spawn {
                if self.car_count as usize >= MAX_CARS {
                    break;
                }
                let c = self.car_count as usize;
                self.spawn_car(c, graph, gravity_active);
                self.car_count += 1;
            }
        }

        // Despawn excess non-seekers (up to 200 per tick for smooth ramp-down)
        if non_seeking > ambient_target {
            let excess = (non_seeking - ambient_target).min(200);
            let mut removed = 0u32;
            let mut i = 0usize;
            while i < self.car_count as usize && removed < excess {
                if !self.car_seeking[i] {
                    self.despawn_car(i);
                    removed += 1;
                    // don't increment i — swap-remove put a new car at i
                } else {
                    i += 1;
                }
            }
        }
    }

    fn spawn_car(&mut self, idx: usize, graph: &Graph, gravity_active: bool) {
        let r = self.rng.next_f32();
        let ei = graph.spawn_edge(r) as usize;
        let dir = if !graph.edge_oneway[ei] && self.rng.next_f32() < 0.5 {
            -1i8
        } else {
            1
        };
        self.car_edge[idx] = ei as u32;
        self.car_dir[idx] = dir;
        self.car_t[idx] = if dir > 0 {
            self.rng.next_f32() * 0.8 + 0.01
        } else {
            0.19 + self.rng.next_f32() * 0.8
        };
        self.car_accel[idx] = 1.0;
        self.car_green_wait[idx] = 0;

        // Parking seeker: 40% of new cars when gravity active
        let seeking = gravity_active && !graph.parking_nodes.is_empty() && self.rng.next_f32() < 0.4;
        self.car_seeking[idx] = seeking;
        if seeking {
            self.car_ttl[idx] = 255; // seekers circulate until they park
            self.seeking_count += 1;
        } else {
            self.car_ttl[idx] = 80 + (self.rng.next_range(176) as u8);
        }

        self.car_jitter[idx] = self.rng.next_f32() * 2.0 - 1.0;
        // Reset trail history
        self.car_trail_head[idx] = 0;
        self.car_trail_len[idx] = 0;
    }

    fn despawn_car(&mut self, idx: usize) {
        // Track seeking count
        if self.car_seeking[idx] {
            self.seeking_count = self.seeking_count.saturating_sub(1);
        }
        // Update follow_idx for swap-remove
        if let Some(fi) = self.follow_idx {
            let fi = fi as usize;
            if fi == idx {
                self.follow_idx = None;
            } else if fi == self.car_count as usize - 1 {
                self.follow_idx = Some(idx as u32);
            }
        }
        self.car_count -= 1;
        let last = self.car_count as usize;
        if idx < last {
            self.car_edge[idx] = self.car_edge[last];
            self.car_dir[idx] = self.car_dir[last];
            self.car_t[idx] = self.car_t[last];
            self.car_accel[idx] = self.car_accel[last];
            self.car_green_wait[idx] = self.car_green_wait[last];
            self.car_ttl[idx] = self.car_ttl[last];
            self.car_jitter[idx] = self.car_jitter[last];
            self.car_seeking[idx] = self.car_seeking[last];
            // Copy trail ring buffer from last to idx
            self.car_trail_head[idx] = self.car_trail_head[last];
            self.car_trail_len[idx] = self.car_trail_len[last];
            let dst = idx * TRAIL_LEN;
            let src = last * TRAIL_LEN;
            for k in 0..TRAIL_LEN {
                self.car_trail_x[dst + k] = self.car_trail_x[src + k];
                self.car_trail_y[dst + k] = self.car_trail_y[src + k];
            }
        }
    }

    fn recycle_car(&mut self, idx: usize, graph: &Graph) {
        self.spawn_car(idx, graph, false);
    }
}

impl SimClock {
    pub fn new(start_hour: f32) -> Self {
        Self {
            sim_time: start_hour,
            current_hour: start_hour.floor() as u32 % 24,
            next_hour: (start_hour.floor() as u32 + 1) % 24,
            hour_blend: start_hour % 1.0,
        }
    }
}

// ── Turn types ──

#[derive(Clone, Copy)]
enum TurnType {
    Left,
    Through,
    Right,
}

/// Classify arrival bearing to approach index.
/// Bearing convention: atan2(dx, dy) where y is down.
///   0 = south (traveling south), π/2 = east, -π/2 = west, ±π = north
///
/// A car traveling eastbound arrives from the west → approach = EB (0)
/// Approach is the direction the car is GOING, matching the turn-ratio convention.
///
/// Returns: 0=EB, 1=WB, 2=NB, 3=SB
#[inline]
fn bearing_to_approach(bearing: f32) -> u32 {
    // bearing ~π/2  → traveling east  → EB approach (0)
    // bearing ~-π/2 → traveling west  → WB approach (1)
    // bearing ~±π   → traveling north → NB approach (2)
    // bearing ~0    → traveling south → SB approach (3)
    let abs_b = bearing.abs();
    if abs_b > std::f32::consts::FRAC_PI_4 && abs_b < 3.0 * std::f32::consts::FRAC_PI_4 {
        // East or West
        if bearing > 0.0 { 0 } else { 1 } // EB : WB
    } else {
        // North or South
        if abs_b > std::f32::consts::FRAC_PI_2 { 2 } else { 3 } // NB : SB
    }
}

/// Congestion-modulated speed for an edge
#[inline(always)]
fn edge_speed_factor(graph: &Graph, ei: usize, clock: &SimClock) -> f32 {
    let base = ei * 24;
    let c0 = graph.edge_congestion[base + clock.current_hour as usize];
    let c1 = graph.edge_congestion[base + clock.next_hour as usize];
    let c = c0 + (c1 - c0) * clock.hour_blend;

    let lo = (c.floor() as usize).min(3);
    let hi = (lo + 1).min(3);
    let frac = c - c.floor();
    CONGESTION_FACTOR[lo] * (1.0 - frac) + CONGESTION_FACTOR[hi] * frac
}

/// Find the nearest stop point ahead of a car on its current edge.
/// Returns Some(stop_t) if there's a red signal or queued car ahead, None if clear.
#[inline]
fn find_stop_point(
    ei: usize,
    dir: i8,
    car_t: f32,
    _gap_t: f32,
    graph: &Graph,
    clock: &SimClock,
    period_off: usize,
    edge_queue_tail_fwd: &[f32],
    edge_queue_tail_rev: &[f32],
) -> Option<f32> {
    // Freeways (speed >= 65 km/h) never stop at traffic signals
    let is_freeway = graph.edge_speed[ei] >= 65.0;

    if dir > 0 {
        let mut stop: Option<f32> = None;

        // Red signal at edge end? (skip for freeways)
        if !is_freeway {
            let node = graph.edge_to[ei] as usize;
            if node < graph.node_count as usize
                && graph.node_signal[node * 8 + period_off] > 0.0
            {
                let bearing = graph.edge_bearing_fwd[ei];
                if !is_signal_green(graph.edge_to[ei], bearing, graph, clock) {
                    stop = Some(0.98);
                }
            }
        }

        // Queue ahead on this edge?
        // edge_queue_tail_fwd stores the t-position the NEXT car should stop at
        if edge_queue_tail_fwd[ei] < 1.1 {
            let queue_stop = edge_queue_tail_fwd[ei].max(0.0).min(0.98);
            if queue_stop > car_t {
                stop = Some(match stop {
                    Some(s) => s.min(queue_stop),
                    None => queue_stop,
                });
            }
        }

        stop
    } else {
        let mut stop: Option<f32> = None;

        // Red signal at edge start? (skip for freeways)
        if !is_freeway {
            let node = graph.edge_from[ei] as usize;
            if node < graph.node_count as usize
                && graph.node_signal[node * 8 + period_off] > 0.0
            {
                let bearing = graph.edge_bearing_rev[ei];
                if !is_signal_green(graph.edge_from[ei], bearing, graph, clock) {
                    stop = Some(0.02);
                }
            }
        }

        // Queue ahead (reverse direction)
        // edge_queue_tail_rev stores the t-position the NEXT car should stop at
        if edge_queue_tail_rev[ei] >= 0.0 {
            let queue_stop = edge_queue_tail_rev[ei].min(1.0).max(0.02);
            if queue_stop < car_t {
                stop = Some(match stop {
                    Some(s) => s.max(queue_stop),
                    None => queue_stop,
                });
            }
        }

        stop
    }
}

/// Fractional edge distance per tick.
/// dt_seconds = actual sim-seconds this tick represents.
/// Returns the fraction of the edge a car traverses in one tick.
#[inline(always)]
fn edge_dt(graph: &Graph, ei: usize, clock: &SimClock, dt_seconds: f32) -> f32 {
    let speed_kmh = graph.edge_speed[ei];
    let cong = edge_speed_factor(graph, ei, clock);
    let total_len_m = graph.edge_total_len[ei].max(1.0) * METERS_PER_PIXEL;
    let speed_ms = speed_kmh * (1000.0 / 3600.0);
    speed_ms * cong * dt_seconds / total_len_m
}

/// Map sim hour to signal period offset (index into 8-element signal array).
#[inline]
pub fn signal_period_offset(hour: u32) -> usize {
    match hour {
        7..=9 => 0,   // AM
        15..=18 => 2, // PM
        10..=14 => 4, // MID
        _ => 6,       // OFF
    }
}

/// Check if a signal is green for a car arriving from `arrival_bearing`.
#[inline]
pub fn is_signal_green(node: u32, arrival_bearing: f32, graph: &Graph, clock: &SimClock) -> bool {
    let period_off = signal_period_offset(clock.current_hour);
    let base = node as usize * 8 + period_off;
    let cycle = graph.node_signal[base];
    let ns_green = graph.node_signal[base + 1];

    if cycle <= 0.0 {
        return true; // no signal data
    }

    // Classify arrival direction as N/S or E/W based on bearing
    let abs_bearing = arrival_bearing.abs();
    let is_ns = abs_bearing < std::f32::consts::FRAC_PI_4
        || abs_bearing > 3.0 * std::f32::consts::FRAC_PI_4;

    let yellow = 3.0;

    // If a direction has no allocated green phase, treat as no signal for that direction
    if is_ns && ns_green <= 0.0 {
        return true; // no N/S phase → N/S traffic passes freely
    }
    let ew_green = cycle - ns_green - 2.0 * yellow;
    if !is_ns && ew_green <= 0.0 {
        return true; // no E/W phase → E/W traffic passes freely
    }

    // Phase timing with per-node offset (prevents all signals from syncing)
    let sim_seconds = clock.sim_time * 3600.0;
    let phase_offset = (node as f32 * 13.7) % cycle;
    let t_in_cycle = (sim_seconds + phase_offset) % cycle;

    if is_ns {
        t_in_cycle < ns_green
    } else {
        t_in_cycle >= ns_green + yellow && t_in_cycle < cycle - yellow
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn congestion_factor_ranges() {
        assert!((CONGESTION_FACTOR[0] - 0.95).abs() < 1e-6);
        assert!((CONGESTION_FACTOR[3] - 0.20).abs() < 1e-6);
    }

    #[test]
    fn sim_clock_init() {
        let clock = SimClock::new(8.5);
        assert_eq!(clock.current_hour, 8);
        assert_eq!(clock.next_hour, 9);
        assert!((clock.hour_blend - 0.5).abs() < 0.01);
    }

    #[test]
    fn bearing_to_approach_cardinal() {
        use std::f32::consts::*;
        // East = π/2 → EB (0)
        assert_eq!(bearing_to_approach(FRAC_PI_2), 0);
        // West = -π/2 → WB (1)
        assert_eq!(bearing_to_approach(-FRAC_PI_2), 1);
        // North = π → NB (2)
        assert_eq!(bearing_to_approach(PI), 2);
        // South = 0 → SB (3)
        assert_eq!(bearing_to_approach(0.0), 3);
    }

    #[test]
    fn data_driven_population() {
        // Population is derived from Little's Law peak_cars (from real count data)
        // target = peak_cars * density/100 * volume_scale
        let peak_cars = 25000.0f32; // typical value from enrich_graph_turns.py
        let density = 65.0;
        // At peak (volume_scale=1.0): 25000 * 0.65 = 16250
        let peak = peak_cars * density / 100.0 * 1.0;
        assert!(peak > 16000.0 && peak < 17000.0);
        // At midnight (volume_scale≈0.15): 25000 * 0.65 * 0.15 = 2437
        let midnight = peak_cars * density / 100.0 * 0.15;
        assert!(midnight > 2000.0 && midnight < 3000.0);
    }
}
