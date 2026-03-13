use crate::graph::METERS_PER_PIXEL;
use crate::sim::{ParticleState, TRAIL_LEN};

/// Per-car speed-based brightness factor from car_accel.
/// Stopped cars are dim, fast cars are bright (drives stronger bloom glow).
#[inline(always)]
fn speed_brightness(accel: f32) -> f32 {
    0.3 + 0.7 * accel
}

/// Fill particle VBOs: trail line segments, body line segments, and head points.
///
/// Vertex format: (x, y, brightness_factor) — 3 floats per vertex.
/// brightness_factor is driven by car_accel so faster cars glow more.
///
/// Line buffer layout: [trail segments...][body segments...]
/// - Trail segments: history ring buffer pairs for each car (dim)
/// - Body segments: 5m car body tail→head for each car (bright)
///
/// Returns (trail_vertex_count, body_vertex_count, point_vertex_count).
pub fn fill_particle_vbo(
    state: &ParticleState,
    trail_length_m: f32,
    line_buf: &mut Vec<f32>,
    point_buf: &mut Vec<f32>,
) -> (u32, u32, u32) {
    let n = state.car_count as usize;
    let trail_px = trail_length_m / METERS_PER_PIXEL;
    let use_trail = trail_length_m > 10.0;

    // Worst case: n * TRAIL_LEN * 6 (trail) + n * 6 (body)  [3 floats/vertex]
    let max_line = if use_trail {
        n * (TRAIL_LEN + 1) * 6
    } else {
        n * 6
    };
    line_buf.clear();
    if line_buf.capacity() < max_line {
        line_buf.reserve(max_line - line_buf.capacity());
    }
    point_buf.clear();
    if point_buf.capacity() < n * 3 {
        point_buf.reserve(n * 3 - point_buf.capacity());
    }

    // Phase 1: emit trail segments (ring buffer history)
    let mut trail_verts = 0u32;
    if use_trail {
        let trail_px_sq = trail_px * trail_px;

        for i in 0..n {
            let tlen = state.car_trail_len[i] as usize;
            if tlen < 2 {
                continue;
            }
            let base = i * TRAIL_LEN;
            let head_idx = state.car_trail_head[i] as usize;
            let hx = state.car_x[i];
            let hy = state.car_y[i];
            let bright = speed_brightness(state.car_accel[i]);

            // Walk backward from newest to oldest, emitting line segments
            let mut prev_x = hx;
            let mut prev_y = hy;
            let mut cum_dist_sq = 0.0f32;

            // Start from most recent history entry (one before head write cursor)
            for k in 1..tlen {
                let ring_idx = (head_idx + TRAIL_LEN - k) % TRAIL_LEN;
                let tx = state.car_trail_x[base + ring_idx];
                let ty = state.car_trail_y[base + ring_idx];

                let dx = prev_x - tx;
                let dy = prev_y - ty;
                let seg_dist_sq = dx * dx + dy * dy;

                // Skip near-zero segments (car was stopped)
                if seg_dist_sq < 0.01 {
                    prev_x = tx;
                    prev_y = ty;
                    continue;
                }

                cum_dist_sq += seg_dist_sq;
                if cum_dist_sq > trail_px_sq {
                    break;
                }

                // Emit segment: trail point → previous point
                line_buf.push(tx);
                line_buf.push(ty);
                line_buf.push(bright);
                line_buf.push(prev_x);
                line_buf.push(prev_y);
                line_buf.push(bright);
                trail_verts += 2;

                prev_x = tx;
                prev_y = ty;
            }
        }
    }

    // Phase 2: emit body segments (5m car body: tail → head)
    for i in 0..n {
        let bright = speed_brightness(state.car_accel[i]);
        line_buf.push(state.car_tx[i]);
        line_buf.push(state.car_ty[i]);
        line_buf.push(bright);
        line_buf.push(state.car_x[i]);
        line_buf.push(state.car_y[i]);
        line_buf.push(bright);
    }
    let body_verts = (n as u32) * 2;

    // Phase 3: head points
    for i in 0..n {
        point_buf.push(state.car_x[i]);
        point_buf.push(state.car_y[i]);
        point_buf.push(speed_brightness(state.car_accel[i]));
    }

    (trail_verts, body_verts, n as u32)
}

/// Fill tiny buffers for the single followed car's highlight.
/// Returns false if the index is out of range.
pub fn fill_highlight_buffers(
    state: &ParticleState,
    idx: u32,
    line_buf: &mut [f32; 4],
    point_buf: &mut [f32; 2],
) -> bool {
    let i = idx as usize;
    if i >= state.car_count as usize {
        return false;
    }
    line_buf[0] = state.car_tx[i];
    line_buf[1] = state.car_ty[i];
    line_buf[2] = state.car_x[i];
    line_buf[3] = state.car_y[i];
    point_buf[0] = state.car_x[i];
    point_buf[1] = state.car_y[i];
    true
}
