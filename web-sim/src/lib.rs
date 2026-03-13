mod bloom;
mod graph;
mod math;
mod particle_mesh;
mod render;
mod road_mesh;
mod shaders;
mod sim;

use graph::Graph;
use render::Renderer;
use sim::{is_signal_green, ParticleState, SimClock, SimParams};
use wasm_bindgen::prelude::*;

#[wasm_bindgen]
pub struct FrameStats {
    pub car_count: u32,
    pub sim_hour: u8,
    pub sim_minute: u8,
    pub following: bool,
    pub gravity_transfers: u32,
    pub parked_count: u32,
    pub seeking_count: u32,
}

#[wasm_bindgen]
pub struct Simulator {
    graph: Graph,
    state: ParticleState,
    clock: SimClock,
    renderer: Renderer,
    params: SimParams,
    brightness: f32,
    bloom_mix: f32,
    gravity_x: f32,
    gravity_y: f32,
    gravity_active: bool,
    gravity_strength: f32,
}

#[wasm_bindgen]
impl Simulator {
    #[wasm_bindgen(constructor)]
    pub fn new(canvas_id: &str, graph_json: &str) -> Result<Simulator, JsValue> {
        console_error_panic_hook_set();

        let graph = Graph::from_json(graph_json).map_err(|e| JsValue::from_str(&e))?;

        let renderer =
            Renderer::new(canvas_id, &graph).map_err(|e| JsValue::from_str(&e))?;

        let mut state = ParticleState::new();
        let params = SimParams {
            density: 65.0,
            day_speed: 0.2,
            trail_length_m: 5.0,
            jitter: 0.0,
        };
        let clock = SimClock::new(8.0);

        state.spawn_initial(&graph, &params);

        web_sys::console::log_1(
            &format!(
                "Flow sim v2: {} edges, {} counted nodes, {} parking nodes ({} spaces), gravity support",
                graph.edge_count,
                graph.counted_nodes.len(),
                graph.parking_nodes.len(),
                graph.node_parking.iter().sum::<u32>(),
            )
            .into(),
        );

        Ok(Simulator {
            graph,
            state,
            clock,
            renderer,
            params,
            brightness: 0.55,
            bloom_mix: 0.55,
            gravity_x: 0.0,
            gravity_y: 0.0,
            gravity_active: false,
            gravity_strength: 50.0,
        })
    }

    /// Run one simulation tick + render frame.
    pub fn frame(&mut self) -> FrameStats {
        self.state.tick(&self.graph, &self.params, &mut self.clock, self.gravity_x, self.gravity_y, self.gravity_active, self.gravity_strength);

        // Update camera if following a car
        let following = if let Some(idx) = self.state.follow_idx {
            let i = idx as usize;
            if i < self.state.car_count as usize {
                self.renderer.set_camera_target(
                    self.state.car_x[i],
                    self.state.car_y[i],
                    4.0,
                );
                true
            } else {
                self.state.follow_idx = None;
                self.renderer.reset_camera();
                false
            }
        } else {
            false
        };

        // Compute signal visualization data
        let mut signal_green: Vec<f32> = Vec::new();
        let mut signal_red: Vec<f32> = Vec::new();
        for &ni in &self.graph.signal_nodes {
            let x = self.graph.node_x[ni as usize];
            let y = self.graph.node_y[ni as usize];
            let ns_green = is_signal_green(ni, 0.0, &self.graph, &self.clock);
            if ns_green {
                signal_red.push(x);
                signal_red.push(y);
            } else {
                signal_green.push(x);
                signal_green.push(y);
            }
        }

        // Compute parking visualization data
        let mut parking_green: Vec<f32> = Vec::new();
        let mut parking_yellow: Vec<f32> = Vec::new();
        let mut parking_red: Vec<f32> = Vec::new();
        for &ni in &self.graph.parking_nodes {
            let occ = self.state.parking_occupancy[ni as usize];
            let cap = self.graph.node_parking[ni as usize];
            let ratio = occ as f32 / cap.max(1) as f32;
            let x = self.graph.node_x[ni as usize];
            let y = self.graph.node_y[ni as usize];
            if ratio > 0.9 {
                parking_red.push(x);
                parking_red.push(y);
            } else if ratio > 0.5 {
                parking_yellow.push(x);
                parking_yellow.push(y);
            } else {
                parking_green.push(x);
                parking_green.push(y);
            }
        }

        // Compute parked car positions (x, y pairs)
        let mut parked_dots: Vec<f32> = Vec::with_capacity(self.state.parked_x.len() * 2);
        for i in 0..self.state.parked_x.len() {
            parked_dots.push(self.state.parked_x[i]);
            parked_dots.push(self.state.parked_y[i]);
        }

        self.renderer.frame(
            &self.state,
            self.brightness,
            self.bloom_mix,
            self.params.trail_length_m,
            self.state.follow_idx,
            &signal_green,
            &signal_red,
            &parking_green,
            &parking_yellow,
            &parking_red,
            &parked_dots,
        );

        let h = (self.clock.sim_time.floor() as u32) % 24;
        let m = ((self.clock.sim_time % 1.0) * 60.0).floor() as u8;

        FrameStats {
            car_count: self.state.car_count,
            sim_hour: h as u8,
            sim_minute: m,
            following,
            gravity_transfers: self.state.gravity_transfers,
            parked_count: self.state.parked_total,
            seeking_count: self.state.seeking_count,
        }
    }

    /// Update a simulation/render parameter from UI slider.
    pub fn set_param(&mut self, name: &str, value: f32) {
        match name {
            "brightness" => self.brightness = value / 100.0,
            "bloom_mix" => self.bloom_mix = value / 100.0,
            "density" => self.params.density = value,
            "day_speed" => self.params.day_speed = value / 100.0,
            "trail" => self.params.trail_length_m = value, // 0-500 meters
            "jitter" => self.params.jitter = value,
            "gravity" => self.gravity_strength = value,
            _ => {}
        }
    }

    pub fn resize(&mut self, width: u32, height: u32) {
        self.renderer.resize(width, height);
    }

    pub fn edge_count(&self) -> u32 {
        self.graph.edge_count
    }

    // ── Follow-car API ──

    /// Follow the car nearest to graph coordinates (gx, gy).
    pub fn follow_car_at(&mut self, graph_x: f32, graph_y: f32) {
        let n = self.state.car_count as usize;
        if n == 0 {
            return;
        }
        let mut best_idx = 0u32;
        let mut best_dist = f32::MAX;
        for i in 0..n {
            let dx = self.state.car_x[i] - graph_x;
            let dy = self.state.car_y[i] - graph_y;
            let d = dx * dx + dy * dy;
            if d < best_dist {
                best_dist = d;
                best_idx = i as u32;
            }
        }
        self.state.follow_idx = Some(best_idx);
    }

    /// Follow a random car.
    pub fn follow_random_car(&mut self) {
        let n = self.state.car_count as usize;
        if n == 0 {
            return;
        }
        self.state.follow_idx = Some(self.state.rng.next_range(n as u32));
    }

    /// Stop following.
    pub fn unfollow(&mut self) {
        self.state.follow_idx = None;
        self.renderer.reset_camera();
    }

    pub fn is_following(&self) -> bool {
        self.state.follow_idx.is_some()
    }

    pub fn cam_x(&self) -> f32 {
        self.renderer.cam_x()
    }

    pub fn cam_y(&self) -> f32 {
        self.renderer.cam_y()
    }

    pub fn cam_zoom(&self) -> f32 {
        self.renderer.cam_zoom()
    }

    // ── Gravity well API ──

    pub fn set_gravity(&mut self, gx: f32, gy: f32) {
        self.gravity_x = gx;
        self.gravity_y = gy;
        self.gravity_active = true;
    }

    pub fn clear_gravity(&mut self) {
        self.gravity_active = false;
    }

}

fn console_error_panic_hook_set() {
    use std::sync::Once;
    static SET_HOOK: Once = Once::new();
    SET_HOOK.call_once(|| {
        std::panic::set_hook(Box::new(|info| {
            web_sys::console::error_1(&format!("WASM panic: {}", info).into());
        }));
    });
}
