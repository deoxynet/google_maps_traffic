use web_sys::{
    WebGl2RenderingContext as GL, WebGlFramebuffer, WebGlProgram, WebGlTexture,
    WebGlUniformLocation,
};

/// Half-resolution ping-pong Gaussian blur pipeline
pub struct BloomPass {
    pub ping_fbo: WebGlFramebuffer,
    pub ping_tex: WebGlTexture,
    pub pong_fbo: WebGlFramebuffer,
    pub pong_tex: WebGlTexture,
    pub half_w: u32,
    pub half_h: u32,
    pub blur_h_program: WebGlProgram,
    pub blur_h_tex_loc: WebGlUniformLocation,
    pub blur_h_texel_loc: WebGlUniformLocation,
    pub blur_v_program: WebGlProgram,
    pub blur_v_tex_loc: WebGlUniformLocation,
    pub blur_v_texel_loc: WebGlUniformLocation,
}

impl BloomPass {
    pub fn new(
        gl: &GL,
        width: u32,
        height: u32,
        blur_h_program: WebGlProgram,
        blur_v_program: WebGlProgram,
    ) -> Result<Self, String> {
        let half_w = width / 2;
        let half_h = height / 2;

        let (ping_fbo, ping_tex) = create_fbo_with_texture(gl, half_w, half_h)?;
        let (pong_fbo, pong_tex) = create_fbo_with_texture(gl, half_w, half_h)?;

        let blur_h_tex_loc = gl
            .get_uniform_location(&blur_h_program, "u_texture")
            .ok_or("blur_h: u_texture")?;
        let blur_h_texel_loc = gl
            .get_uniform_location(&blur_h_program, "u_texel_size")
            .ok_or("blur_h: u_texel_size")?;
        let blur_v_tex_loc = gl
            .get_uniform_location(&blur_v_program, "u_texture")
            .ok_or("blur_v: u_texture")?;
        let blur_v_texel_loc = gl
            .get_uniform_location(&blur_v_program, "u_texel_size")
            .ok_or("blur_v: u_texel_size")?;

        Ok(Self {
            ping_fbo,
            ping_tex,
            pong_fbo,
            pong_tex,
            half_w,
            half_h,
            blur_h_program,
            blur_h_tex_loc,
            blur_h_texel_loc,
            blur_v_program,
            blur_v_tex_loc,
            blur_v_texel_loc,
        })
    }

    /// Run multi-pass separable blur: source_tex → pong (H) → ping (V), repeated.
    /// More iterations = wider, softer glow. After this, ping_tex contains the result.
    pub fn run(
        &self,
        gl: &GL,
        source_tex: &WebGlTexture,
        empty_vao: &web_sys::WebGlVertexArrayObject,
        iterations: u32,
    ) {
        let tx = 1.0 / self.half_w as f32;
        let ty = 1.0 / self.half_h as f32;
        let hw = self.half_w as i32;
        let hh = self.half_h as i32;

        let iterations = iterations.max(1).min(5);

        for pass in 0..iterations {
            // Horizontal blur → pong_fbo
            gl.bind_framebuffer(GL::FRAMEBUFFER, Some(&self.pong_fbo));
            gl.viewport(0, 0, hw, hh);
            gl.clear(GL::COLOR_BUFFER_BIT);

            gl.use_program(Some(&self.blur_h_program));
            gl.active_texture(GL::TEXTURE0);
            // First iteration reads from source, subsequent from ping (previous result)
            if pass == 0 {
                gl.bind_texture(GL::TEXTURE_2D, Some(source_tex));
            } else {
                gl.bind_texture(GL::TEXTURE_2D, Some(&self.ping_tex));
            }
            gl.uniform1i(Some(&self.blur_h_tex_loc), 0);
            gl.uniform2f(Some(&self.blur_h_texel_loc), tx, ty);

            gl.bind_vertex_array(Some(empty_vao));
            gl.draw_arrays(GL::TRIANGLES, 0, 3);

            // Vertical blur → ping_fbo
            gl.bind_framebuffer(GL::FRAMEBUFFER, Some(&self.ping_fbo));
            gl.viewport(0, 0, hw, hh);
            gl.clear(GL::COLOR_BUFFER_BIT);

            gl.use_program(Some(&self.blur_v_program));
            gl.active_texture(GL::TEXTURE0);
            gl.bind_texture(GL::TEXTURE_2D, Some(&self.pong_tex));
            gl.uniform1i(Some(&self.blur_v_tex_loc), 0);
            gl.uniform2f(Some(&self.blur_v_texel_loc), tx, ty);

            gl.bind_vertex_array(Some(empty_vao));
            gl.draw_arrays(GL::TRIANGLES, 0, 3);
        }
    }

    pub fn resize(&mut self, gl: &GL, width: u32, height: u32) -> Result<(), String> {
        self.half_w = width / 2;
        self.half_h = height / 2;

        resize_fbo_texture(gl, &self.ping_fbo, &self.ping_tex, self.half_w, self.half_h);
        resize_fbo_texture(gl, &self.pong_fbo, &self.pong_tex, self.half_w, self.half_h);

        Ok(())
    }
}

/// Full-resolution FBO for particle rendering (before bloom)
pub fn create_fbo_with_texture(
    gl: &GL,
    width: u32,
    height: u32,
) -> Result<(WebGlFramebuffer, WebGlTexture), String> {
    let fbo = gl.create_framebuffer().ok_or("create fbo")?;
    let tex = gl.create_texture().ok_or("create tex")?;

    gl.bind_texture(GL::TEXTURE_2D, Some(&tex));
    gl.tex_image_2d_with_i32_and_i32_and_i32_and_format_and_type_and_opt_u8_array(
        GL::TEXTURE_2D,
        0,
        GL::RGBA16F as i32,
        width as i32,
        height as i32,
        0,
        GL::RGBA,
        GL::HALF_FLOAT,
        None,
    )
    .map_err(|e| format!("tex_image_2d: {:?}", e))?;
    gl.tex_parameteri(GL::TEXTURE_2D, GL::TEXTURE_MIN_FILTER, GL::LINEAR as i32);
    gl.tex_parameteri(GL::TEXTURE_2D, GL::TEXTURE_MAG_FILTER, GL::LINEAR as i32);
    gl.tex_parameteri(GL::TEXTURE_2D, GL::TEXTURE_WRAP_S, GL::CLAMP_TO_EDGE as i32);
    gl.tex_parameteri(GL::TEXTURE_2D, GL::TEXTURE_WRAP_T, GL::CLAMP_TO_EDGE as i32);

    gl.bind_framebuffer(GL::FRAMEBUFFER, Some(&fbo));
    gl.framebuffer_texture_2d(
        GL::FRAMEBUFFER,
        GL::COLOR_ATTACHMENT0,
        GL::TEXTURE_2D,
        Some(&tex),
        0,
    );

    gl.bind_framebuffer(GL::FRAMEBUFFER, None);
    gl.bind_texture(GL::TEXTURE_2D, None);

    Ok((fbo, tex))
}

pub fn resize_fbo_texture_pub(gl: &GL, fbo: &WebGlFramebuffer, tex: &WebGlTexture, w: u32, h: u32) {
    resize_fbo_texture(gl, fbo, tex, w, h);
}

fn resize_fbo_texture(gl: &GL, fbo: &WebGlFramebuffer, tex: &WebGlTexture, w: u32, h: u32) {
    gl.bind_texture(GL::TEXTURE_2D, Some(tex));
    let _ = gl.tex_image_2d_with_i32_and_i32_and_i32_and_format_and_type_and_opt_u8_array(
        GL::TEXTURE_2D,
        0,
        GL::RGBA16F as i32,
        w as i32,
        h as i32,
        0,
        GL::RGBA,
        GL::HALF_FLOAT,
        None,
    );
    gl.bind_framebuffer(GL::FRAMEBUFFER, Some(fbo));
    gl.framebuffer_texture_2d(
        GL::FRAMEBUFFER,
        GL::COLOR_ATTACHMENT0,
        GL::TEXTURE_2D,
        Some(tex),
        0,
    );
    gl.bind_framebuffer(GL::FRAMEBUFFER, None);
    gl.bind_texture(GL::TEXTURE_2D, None);
}
