use wasm_bindgen::JsCast;
use web_sys::{
    WebGl2RenderingContext as GL, WebGlBuffer, WebGlFramebuffer, WebGlProgram, WebGlTexture,
    WebGlUniformLocation, WebGlVertexArrayObject,
};

use crate::bloom::{self, BloomPass};
use crate::graph::Graph;
use crate::particle_mesh;
use crate::road_mesh;
use crate::shaders;
use crate::sim::ParticleState;

pub struct Renderer {
    gl: GL,
    width: u32,
    height: u32,

    // Camera
    cam_x: f32,
    cam_y: f32,
    cam_zoom: f32,
    target_x: f32,
    target_y: f32,
    target_zoom: f32,

    // Road pass
    road_program: WebGlProgram,
    road_vao: WebGlVertexArrayObject,
    road_vertex_count: u32,
    road_res_loc: WebGlUniformLocation,
    road_pan_loc: WebGlUniformLocation,
    road_zoom_loc: WebGlUniformLocation,

    // Particle pass
    particle_program: WebGlProgram,
    particle_vao_lines: WebGlVertexArrayObject,
    particle_vao_points: WebGlVertexArrayObject,
    particle_line_vbo: WebGlBuffer,
    particle_point_vbo: WebGlBuffer,
    particle_res_loc: WebGlUniformLocation,
    particle_pan_loc: WebGlUniformLocation,
    particle_zoom_loc: WebGlUniformLocation,
    particle_bright_loc: WebGlUniformLocation,
    particle_point_size_loc: WebGlUniformLocation,
    particle_color_loc: WebGlUniformLocation,

    // Highlight (followed car)
    highlight_line_vbo: WebGlBuffer,
    highlight_point_vbo: WebGlBuffer,
    highlight_vao_lines: WebGlVertexArrayObject,
    highlight_vao_points: WebGlVertexArrayObject,

    // Particle FBO (full resolution, render target for particles)
    particle_fbo: WebGlFramebuffer,
    particle_tex: WebGlTexture,

    // Bloom
    bloom: BloomPass,

    // Composite pass
    composite_program: WebGlProgram,
    composite_particles_loc: WebGlUniformLocation,
    composite_bloom_loc: WebGlUniformLocation,
    composite_bloom_mix_loc: WebGlUniformLocation,

    // Empty VAO for fullscreen triangle draws
    empty_vao: WebGlVertexArrayObject,

    // Signal visualization
    signal_vbo: WebGlBuffer,
    signal_vao: WebGlVertexArrayObject,

    // Parking visualization (reuses signal_vbo/signal_vao pattern)
    parking_vbo: WebGlBuffer,
    parking_vao: WebGlVertexArrayObject,

    // Parked car dots (x, y per vertex)
    parked_vbo: WebGlBuffer,
    parked_vao: WebGlVertexArrayObject,

    // Reusable buffers (avoid allocation per frame)
    line_buf: Vec<f32>,
    point_buf: Vec<f32>,
}

impl Renderer {
    pub fn new(canvas_id: &str, graph: &Graph) -> Result<Self, String> {
        let document = web_sys::window()
            .ok_or("no window")?
            .document()
            .ok_or("no document")?;
        let canvas = document
            .get_element_by_id(canvas_id)
            .ok_or("canvas not found")?
            .dyn_into::<web_sys::HtmlCanvasElement>()
            .map_err(|_| "not a canvas")?;

        canvas.set_width(graph.width);
        canvas.set_height(graph.height);

        let gl: GL = canvas
            .get_context_with_context_options("webgl2", &{
                let opts = js_sys::Object::new();
                js_sys::Reflect::set(&opts, &"alpha".into(), &false.into()).ok();
                js_sys::Reflect::set(&opts, &"antialias".into(), &false.into()).ok();
                js_sys::Reflect::set(&opts, &"premultipliedAlpha".into(), &false.into()).ok();
                opts.into()
            })
            .map_err(|e| format!("{:?}", e))?
            .ok_or("webgl2 not available")?
            .dyn_into::<GL>()
            .map_err(|_| "not webgl2")?;

        // Check color buffer float support
        gl.get_extension("EXT_color_buffer_float")
            .map_err(|_| "EXT_color_buffer_float not available")?;

        let width = graph.width;
        let height = graph.height;

        // ── Compile shader programs ──
        let road_program = compile_program(&gl, shaders::ROAD_VERT, shaders::ROAD_FRAG)?;
        let particle_program =
            compile_program(&gl, shaders::PARTICLE_VERT, shaders::PARTICLE_FRAG)?;
        let blur_h_program =
            compile_program(&gl, shaders::FULLSCREEN_VERT, shaders::BLUR_H_FRAG)?;
        let blur_v_program =
            compile_program(&gl, shaders::FULLSCREEN_VERT, shaders::BLUR_V_FRAG)?;
        let composite_program =
            compile_program(&gl, shaders::FULLSCREEN_VERT, shaders::COMPOSITE_FRAG)?;

        // ── Build static road mesh ──
        let (road_data, road_vertex_count) = road_mesh::build_road_vbo(graph);
        let road_vbo = gl.create_buffer().ok_or("road vbo")?;
        let road_vao = gl.create_vertex_array().ok_or("road vao")?;
        gl.bind_vertex_array(Some(&road_vao));
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&road_vbo));
        upload_f32_buffer(&gl, GL::ARRAY_BUFFER, &road_data, GL::STATIC_DRAW);
        let road_pos_loc = gl.get_attrib_location(&road_program, "a_position") as u32;
        gl.enable_vertex_attrib_array(road_pos_loc);
        gl.vertex_attrib_pointer_with_i32(road_pos_loc, 2, GL::FLOAT, false, 0, 0);
        gl.bind_vertex_array(None);

        let road_res_loc = gl
            .get_uniform_location(&road_program, "u_resolution")
            .ok_or("road u_resolution")?;
        let road_pan_loc = gl
            .get_uniform_location(&road_program, "u_pan")
            .ok_or("road u_pan")?;
        let road_zoom_loc = gl
            .get_uniform_location(&road_program, "u_zoom")
            .ok_or("road u_zoom")?;

        // ── Particle buffers (dynamic) ──
        let particle_line_vbo = gl.create_buffer().ok_or("particle line vbo")?;
        let particle_point_vbo = gl.create_buffer().ok_or("particle point vbo")?;

        let particle_pos_loc = gl.get_attrib_location(&particle_program, "a_position") as u32;
        let particle_bright_attr = gl.get_attrib_location(&particle_program, "a_brightness") as u32;

        // Set default brightness for VAOs that don't enable the attribute (signals, highlights)
        gl.vertex_attrib1f(particle_bright_attr, 1.0);

        // VAO for trail lines (vertex format: x, y, brightness — stride=12)
        let particle_vao_lines = gl.create_vertex_array().ok_or("particle line vao")?;
        gl.bind_vertex_array(Some(&particle_vao_lines));
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&particle_line_vbo));
        gl.enable_vertex_attrib_array(particle_pos_loc);
        gl.vertex_attrib_pointer_with_i32(particle_pos_loc, 2, GL::FLOAT, false, 12, 0);
        gl.enable_vertex_attrib_array(particle_bright_attr);
        gl.vertex_attrib_pointer_with_i32(particle_bright_attr, 1, GL::FLOAT, false, 12, 8);
        gl.bind_vertex_array(None);

        // VAO for head dots (vertex format: x, y, brightness — stride=12)
        let particle_vao_points = gl.create_vertex_array().ok_or("particle point vao")?;
        gl.bind_vertex_array(Some(&particle_vao_points));
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&particle_point_vbo));
        gl.enable_vertex_attrib_array(particle_pos_loc);
        gl.vertex_attrib_pointer_with_i32(particle_pos_loc, 2, GL::FLOAT, false, 12, 0);
        gl.enable_vertex_attrib_array(particle_bright_attr);
        gl.vertex_attrib_pointer_with_i32(particle_bright_attr, 1, GL::FLOAT, false, 12, 8);
        gl.bind_vertex_array(None);

        let particle_res_loc = gl
            .get_uniform_location(&particle_program, "u_resolution")
            .ok_or("particle u_resolution")?;
        let particle_pan_loc = gl
            .get_uniform_location(&particle_program, "u_pan")
            .ok_or("particle u_pan")?;
        let particle_zoom_loc = gl
            .get_uniform_location(&particle_program, "u_zoom")
            .ok_or("particle u_zoom")?;
        let particle_bright_loc = gl
            .get_uniform_location(&particle_program, "u_brightness")
            .ok_or("particle u_brightness")?;
        let particle_point_size_loc = gl
            .get_uniform_location(&particle_program, "u_point_size")
            .ok_or("particle u_point_size")?;
        let particle_color_loc = gl
            .get_uniform_location(&particle_program, "u_color")
            .ok_or("particle u_color")?;

        // ── Highlight VBOs + VAOs (followed car) ──
        let highlight_line_vbo = gl.create_buffer().ok_or("highlight line vbo")?;
        let highlight_point_vbo = gl.create_buffer().ok_or("highlight point vbo")?;

        let highlight_vao_lines = gl.create_vertex_array().ok_or("highlight line vao")?;
        gl.bind_vertex_array(Some(&highlight_vao_lines));
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&highlight_line_vbo));
        gl.enable_vertex_attrib_array(particle_pos_loc);
        gl.vertex_attrib_pointer_with_i32(particle_pos_loc, 2, GL::FLOAT, false, 0, 0);
        gl.bind_vertex_array(None);

        let highlight_vao_points = gl.create_vertex_array().ok_or("highlight point vao")?;
        gl.bind_vertex_array(Some(&highlight_vao_points));
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&highlight_point_vbo));
        gl.enable_vertex_attrib_array(particle_pos_loc);
        gl.vertex_attrib_pointer_with_i32(particle_pos_loc, 2, GL::FLOAT, false, 0, 0);
        gl.bind_vertex_array(None);

        // ── Signal VBO + VAO ──
        let signal_vbo = gl.create_buffer().ok_or("signal vbo")?;
        let signal_vao = gl.create_vertex_array().ok_or("signal vao")?;
        gl.bind_vertex_array(Some(&signal_vao));
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&signal_vbo));
        gl.enable_vertex_attrib_array(particle_pos_loc);
        gl.vertex_attrib_pointer_with_i32(particle_pos_loc, 2, GL::FLOAT, false, 0, 0);
        gl.bind_vertex_array(None);

        // ── Parking VBO + VAO ──
        let parking_vbo = gl.create_buffer().ok_or("parking vbo")?;
        let parking_vao = gl.create_vertex_array().ok_or("parking vao")?;
        gl.bind_vertex_array(Some(&parking_vao));
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&parking_vbo));
        gl.enable_vertex_attrib_array(particle_pos_loc);
        gl.vertex_attrib_pointer_with_i32(particle_pos_loc, 2, GL::FLOAT, false, 0, 0);
        gl.bind_vertex_array(None);

        // ── Parked car VBO + VAO (x, y — stride=8, same as signals/parking) ──
        let parked_vbo = gl.create_buffer().ok_or("parked vbo")?;
        let parked_vao = gl.create_vertex_array().ok_or("parked vao")?;
        gl.bind_vertex_array(Some(&parked_vao));
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&parked_vbo));
        gl.enable_vertex_attrib_array(particle_pos_loc);
        gl.vertex_attrib_pointer_with_i32(particle_pos_loc, 2, GL::FLOAT, false, 0, 0);
        gl.bind_vertex_array(None);

        // ── Particle FBO (full resolution) ──
        let (particle_fbo, particle_tex) = bloom::create_fbo_with_texture(&gl, width, height)?;

        // ── Bloom pipeline ──
        let bloom = BloomPass::new(&gl, width, height, blur_h_program, blur_v_program)?;

        // ── Composite uniforms ──
        let composite_particles_loc = gl
            .get_uniform_location(&composite_program, "u_particles")
            .ok_or("composite u_particles")?;
        let composite_bloom_loc = gl
            .get_uniform_location(&composite_program, "u_bloom")
            .ok_or("composite u_bloom")?;
        let composite_bloom_mix_loc = gl
            .get_uniform_location(&composite_program, "u_bloom_mix")
            .ok_or("composite u_bloom_mix")?;

        // ── Empty VAO for fullscreen triangle ──
        let empty_vao = gl.create_vertex_array().ok_or("empty vao")?;

        Ok(Renderer {
            gl,
            width,
            height,
            cam_x: width as f32 / 2.0,
            cam_y: height as f32 / 2.0,
            cam_zoom: 1.0,
            target_x: width as f32 / 2.0,
            target_y: height as f32 / 2.0,
            target_zoom: 1.0,
            road_program,
            road_vao,
            road_vertex_count,
            road_res_loc,
            road_pan_loc,
            road_zoom_loc,
            particle_program,
            particle_vao_lines,
            particle_vao_points,
            particle_line_vbo,
            particle_point_vbo,
            particle_res_loc,
            particle_pan_loc,
            particle_zoom_loc,
            particle_bright_loc,
            particle_point_size_loc,
            particle_color_loc,
            highlight_line_vbo,
            highlight_point_vbo,
            highlight_vao_lines,
            highlight_vao_points,
            particle_fbo,
            particle_tex,
            bloom,
            composite_program,
            composite_particles_loc,
            composite_bloom_loc,
            composite_bloom_mix_loc,
            signal_vbo,
            signal_vao,
            parking_vbo,
            parking_vao,
            parked_vbo,
            parked_vao,
            empty_vao,
            line_buf: Vec::new(),
            point_buf: Vec::new(),
        })
    }

    pub fn frame(
        &mut self,
        state: &ParticleState,
        brightness: f32,
        bloom_mix: f32,
        trail_length_m: f32,
        follow_idx: Option<u32>,
        signal_green: &[f32],
        signal_red: &[f32],
        parking_green: &[f32],
        parking_yellow: &[f32],
        parking_red: &[f32],
        parked_dots: &[f32],  // x, y pairs for parked cars
    ) {
        // ── Lerp camera ──
        self.cam_x += (self.target_x - self.cam_x) * 0.05;
        self.cam_y += (self.target_y - self.cam_y) * 0.05;
        self.cam_zoom += (self.target_zoom - self.cam_zoom) * 0.05;

        let gl = &self.gl;
        let w = self.width as i32;
        let h = self.height as i32;
        let wf = self.width as f32;
        let hf = self.height as f32;

        // ── Fill particle VBOs ──
        let (trail_vert_count, body_vert_count, point_count) =
            particle_mesh::fill_particle_vbo(state, trail_length_m, &mut self.line_buf, &mut self.point_buf);

        // Upload line data
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.particle_line_vbo));
        upload_f32_buffer(gl, GL::ARRAY_BUFFER, &self.line_buf, GL::DYNAMIC_DRAW);

        // Upload point data
        gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.particle_point_vbo));
        upload_f32_buffer(gl, GL::ARRAY_BUFFER, &self.point_buf, GL::DYNAMIC_DRAW);

        // ── Pass 1: Clear + Roads (to default framebuffer) ──
        gl.bind_framebuffer(GL::FRAMEBUFFER, None);
        gl.viewport(0, 0, w, h);
        gl.clear_color(3.0 / 255.0, 3.0 / 255.0, 12.0 / 255.0, 1.0);
        gl.clear(GL::COLOR_BUFFER_BIT);

        // Roads: alpha blending
        gl.enable(GL::BLEND);
        gl.blend_func(GL::SRC_ALPHA, GL::ONE_MINUS_SRC_ALPHA);

        gl.use_program(Some(&self.road_program));
        gl.uniform2f(Some(&self.road_res_loc), wf, hf);
        gl.uniform2f(Some(&self.road_pan_loc), self.cam_x, self.cam_y);
        gl.uniform1f(Some(&self.road_zoom_loc), self.cam_zoom);
        gl.bind_vertex_array(Some(&self.road_vao));
        gl.draw_arrays(GL::LINES, 0, self.road_vertex_count as i32);

        // ── Pass 2: Particles → FBO ──
        gl.bind_framebuffer(GL::FRAMEBUFFER, Some(&self.particle_fbo));
        gl.viewport(0, 0, w, h);
        gl.clear_color(0.0, 0.0, 0.0, 0.0);
        gl.clear(GL::COLOR_BUFFER_BIT);

        // Additive blending for particles
        gl.blend_func(GL::ONE, GL::ONE);

        gl.use_program(Some(&self.particle_program));
        gl.uniform2f(Some(&self.particle_res_loc), wf, hf);
        gl.uniform2f(Some(&self.particle_pan_loc), self.cam_x, self.cam_y);
        gl.uniform1f(Some(&self.particle_zoom_loc), self.cam_zoom);

        // Default cyan color
        gl.uniform3f(
            Some(&self.particle_color_loc),
            25.0 / 255.0,
            210.0 / 255.0,
            255.0 / 255.0,
        );

        // Trail history lines (dimmer, thinner)
        gl.bind_vertex_array(Some(&self.particle_vao_lines));
        if trail_vert_count > 0 {
            gl.uniform1f(Some(&self.particle_bright_loc), brightness * 0.35);
            gl.uniform1f(Some(&self.particle_point_size_loc), 1.0);
            gl.line_width(1.0);
            gl.draw_arrays(GL::LINES, 0, trail_vert_count as i32);
        }

        // Car body lines (full brightness)
        gl.uniform1f(Some(&self.particle_bright_loc), brightness);
        gl.line_width(2.0);
        gl.draw_arrays(GL::LINES, trail_vert_count as i32, body_vert_count as i32);

        // Head dots (brighter)
        gl.uniform1f(Some(&self.particle_bright_loc), brightness * 1.5);
        gl.uniform1f(Some(&self.particle_point_size_loc), 2.0);
        gl.bind_vertex_array(Some(&self.particle_vao_points));
        gl.draw_arrays(GL::POINTS, 0, point_count as i32);

        // ── Signal dots (green/red at intersections) ──
        // Scale point size and brightness with zoom: subtle at overview, visible when zoomed
        let sig_size = (2.0 * self.cam_zoom).clamp(2.0, 4.0);
        let sig_bright = (0.35 * self.cam_zoom).clamp(0.35, 0.7);
        gl.uniform1f(Some(&self.particle_point_size_loc), sig_size);
        gl.bind_vertex_array(Some(&self.signal_vao));

        if !signal_green.is_empty() {
            gl.uniform3f(Some(&self.particle_color_loc), 0.1, 0.9, 0.2);
            gl.uniform1f(Some(&self.particle_bright_loc), sig_bright);
            gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.signal_vbo));
            upload_f32_buffer(gl, GL::ARRAY_BUFFER, signal_green, GL::DYNAMIC_DRAW);
            gl.draw_arrays(GL::POINTS, 0, (signal_green.len() / 2) as i32);
        }
        if !signal_red.is_empty() {
            gl.uniform3f(Some(&self.particle_color_loc), 0.9, 0.15, 0.1);
            gl.uniform1f(Some(&self.particle_bright_loc), sig_bright);
            gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.signal_vbo));
            upload_f32_buffer(gl, GL::ARRAY_BUFFER, signal_red, GL::DYNAMIC_DRAW);
            gl.draw_arrays(GL::POINTS, 0, (signal_red.len() / 2) as i32);
        }

        // ── Parking dots (green/yellow/red for availability) ──
        let park_size = (3.0 * self.cam_zoom).clamp(3.0, 8.0);
        let park_bright = (0.5 * self.cam_zoom).clamp(0.4, 0.8);
        gl.uniform1f(Some(&self.particle_point_size_loc), park_size);
        gl.bind_vertex_array(Some(&self.parking_vao));

        if !parking_green.is_empty() {
            gl.uniform3f(Some(&self.particle_color_loc), 0.1, 0.8, 0.3);
            gl.uniform1f(Some(&self.particle_bright_loc), park_bright);
            gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.parking_vbo));
            upload_f32_buffer(gl, GL::ARRAY_BUFFER, parking_green, GL::DYNAMIC_DRAW);
            gl.draw_arrays(GL::POINTS, 0, (parking_green.len() / 2) as i32);
        }
        if !parking_yellow.is_empty() {
            gl.uniform3f(Some(&self.particle_color_loc), 0.9, 0.8, 0.1);
            gl.uniform1f(Some(&self.particle_bright_loc), park_bright);
            gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.parking_vbo));
            upload_f32_buffer(gl, GL::ARRAY_BUFFER, parking_yellow, GL::DYNAMIC_DRAW);
            gl.draw_arrays(GL::POINTS, 0, (parking_yellow.len() / 2) as i32);
        }
        if !parking_red.is_empty() {
            gl.uniform3f(Some(&self.particle_color_loc), 0.9, 0.15, 0.1);
            gl.uniform1f(Some(&self.particle_bright_loc), park_bright);
            gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.parking_vbo));
            upload_f32_buffer(gl, GL::ARRAY_BUFFER, parking_red, GL::DYNAMIC_DRAW);
            gl.draw_arrays(GL::POINTS, 0, (parking_red.len() / 2) as i32);
        }

        // ── Parked car dots (green) ──
        if !parked_dots.is_empty() {
            let parked_count = parked_dots.len() / 2;
            let parked_size = (3.0 * self.cam_zoom).clamp(2.0, 6.0);
            gl.uniform1f(Some(&self.particle_point_size_loc), parked_size);
            gl.uniform3f(Some(&self.particle_color_loc), 0.1, 0.9, 0.2);
            gl.uniform1f(Some(&self.particle_bright_loc), park_bright);
            gl.bind_vertex_array(Some(&self.parked_vao));
            gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.parked_vbo));
            upload_f32_buffer(gl, GL::ARRAY_BUFFER, parked_dots, GL::DYNAMIC_DRAW);
            gl.draw_arrays(GL::POINTS, 0, parked_count as i32);
        }

        // ── Highlight followed car ──
        if let Some(fi) = follow_idx {
            let mut hl_line = [0.0f32; 4];
            let mut hl_point = [0.0f32; 2];
            if particle_mesh::fill_highlight_buffers(state, fi, &mut hl_line, &mut hl_point) {
                gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.highlight_line_vbo));
                upload_f32_buffer(gl, GL::ARRAY_BUFFER, &hl_line, GL::DYNAMIC_DRAW);

                gl.bind_buffer(GL::ARRAY_BUFFER, Some(&self.highlight_point_vbo));
                upload_f32_buffer(gl, GL::ARRAY_BUFFER, &hl_point, GL::DYNAMIC_DRAW);

                // Gold color
                gl.uniform3f(Some(&self.particle_color_loc), 1.0, 0.7, 0.2);

                // Trail
                gl.uniform1f(Some(&self.particle_bright_loc), brightness * 2.0);
                gl.uniform1f(Some(&self.particle_point_size_loc), 2.0);
                gl.line_width(3.0);
                gl.bind_vertex_array(Some(&self.highlight_vao_lines));
                gl.draw_arrays(GL::LINES, 0, 2);

                // Head dot
                gl.uniform1f(Some(&self.particle_bright_loc), brightness * 2.5);
                gl.uniform1f(Some(&self.particle_point_size_loc), 4.0);
                gl.bind_vertex_array(Some(&self.highlight_vao_points));
                gl.draw_arrays(GL::POINTS, 0, 1);
            }
        }

        // ── Pass 3: Bloom (half-res, multi-pass for wider glow) ──
        gl.disable(GL::BLEND);
        // More bloom mix → more blur iterations for wider atmospheric glow
        let bloom_iters = if bloom_mix > 2.0 { 3 } else if bloom_mix > 0.8 { 2 } else { 1 };
        self.bloom.run(gl, &self.particle_tex, &self.empty_vao, bloom_iters);

        // ── Pass 4: Composite to screen ──
        gl.bind_framebuffer(GL::FRAMEBUFFER, None);
        gl.viewport(0, 0, w, h);
        // Don't clear — roads are already on the default framebuffer

        // Additive blend composite over roads
        gl.enable(GL::BLEND);
        gl.blend_func(GL::ONE, GL::ONE);

        gl.use_program(Some(&self.composite_program));

        gl.active_texture(GL::TEXTURE0);
        gl.bind_texture(GL::TEXTURE_2D, Some(&self.particle_tex));
        gl.uniform1i(Some(&self.composite_particles_loc), 0);

        gl.active_texture(GL::TEXTURE1);
        gl.bind_texture(GL::TEXTURE_2D, Some(&self.bloom.ping_tex));
        gl.uniform1i(Some(&self.composite_bloom_loc), 1);

        gl.uniform1f(Some(&self.composite_bloom_mix_loc), bloom_mix);

        gl.bind_vertex_array(Some(&self.empty_vao));
        gl.draw_arrays(GL::TRIANGLES, 0, 3);

        // Reset
        gl.disable(GL::BLEND);
        gl.bind_vertex_array(None);
    }

    pub fn set_camera_target(&mut self, x: f32, y: f32, zoom: f32) {
        self.target_x = x;
        self.target_y = y;
        self.target_zoom = zoom;
    }

    pub fn reset_camera(&mut self) {
        self.target_x = self.width as f32 / 2.0;
        self.target_y = self.height as f32 / 2.0;
        self.target_zoom = 1.0;
    }

    pub fn cam_x(&self) -> f32 {
        self.cam_x
    }
    pub fn cam_y(&self) -> f32 {
        self.cam_y
    }
    pub fn cam_zoom(&self) -> f32 {
        self.cam_zoom
    }

    pub fn resize(&mut self, width: u32, height: u32) {
        // If camera is at default (not following), update center
        if (self.target_zoom - 1.0).abs() < 0.01 {
            self.cam_x = width as f32 / 2.0;
            self.cam_y = height as f32 / 2.0;
            self.target_x = width as f32 / 2.0;
            self.target_y = height as f32 / 2.0;
        }
        self.width = width;
        self.height = height;
        // Resize particle FBO
        bloom::resize_fbo_texture_pub(&self.gl, &self.particle_fbo, &self.particle_tex, width, height);
        // Resize bloom FBOs
        let _ = self.bloom.resize(&self.gl, width, height);
    }
}

// ── WebGL helpers ────────────────────────────────────────────────────

fn compile_shader(gl: &GL, shader_type: u32, source: &str) -> Result<web_sys::WebGlShader, String> {
    let shader = gl.create_shader(shader_type).ok_or("create shader")?;
    gl.shader_source(&shader, source);
    gl.compile_shader(&shader);

    if gl
        .get_shader_parameter(&shader, GL::COMPILE_STATUS)
        .as_bool()
        .unwrap_or(false)
    {
        Ok(shader)
    } else {
        let log = gl.get_shader_info_log(&shader).unwrap_or_default();
        gl.delete_shader(Some(&shader));
        Err(format!("shader compile: {}", log))
    }
}

fn compile_program(gl: &GL, vert_src: &str, frag_src: &str) -> Result<WebGlProgram, String> {
    let vert = compile_shader(gl, GL::VERTEX_SHADER, vert_src)?;
    let frag = compile_shader(gl, GL::FRAGMENT_SHADER, frag_src)?;

    let program = gl.create_program().ok_or("create program")?;
    gl.attach_shader(&program, &vert);
    gl.attach_shader(&program, &frag);
    gl.link_program(&program);

    if gl
        .get_program_parameter(&program, GL::LINK_STATUS)
        .as_bool()
        .unwrap_or(false)
    {
        // Shaders can be deleted after linking
        gl.delete_shader(Some(&vert));
        gl.delete_shader(Some(&frag));
        Ok(program)
    } else {
        let log = gl.get_program_info_log(&program).unwrap_or_default();
        gl.delete_program(Some(&program));
        Err(format!("program link: {}", log))
    }
}

fn upload_f32_buffer(gl: &GL, target: u32, data: &[f32], usage: u32) {
    unsafe {
        let view = js_sys::Float32Array::view(data);
        gl.buffer_data_with_array_buffer_view(target, &view, usage);
    }
}
