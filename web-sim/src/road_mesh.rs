use crate::graph::Graph;

/// Build a flat f32 buffer of (x, y) pairs for GL_LINES.
/// Each edge polyline segment becomes a line: [x0, y0, x1, y1, ...].
/// Returns (vertex_data, vertex_count).
pub fn build_road_vbo(graph: &Graph) -> (Vec<f32>, u32) {
    // Estimate: each edge has avg ~4 pts → ~3 segments → 6 vertices → 12 floats
    let estimated = graph.edge_count as usize * 12;
    let mut data = Vec::with_capacity(estimated);
    let mut count = 0u32;

    for ei in 0..graph.edge_count as usize {
        let off = graph.pts_offset[ei] as usize;
        let end = graph.pts_offset[ei + 1] as usize;
        let n = end - off;
        if n < 2 {
            continue;
        }
        for j in 0..(n - 1) {
            // Line segment: point j → point j+1
            data.push(graph.pts_x[off + j]);
            data.push(graph.pts_y[off + j]);
            data.push(graph.pts_x[off + j + 1]);
            data.push(graph.pts_y[off + j + 1]);
            count += 2;
        }
    }

    (data, count)
}
