// ── Road shader ──────────────────────────────────────────────────────

pub const ROAD_VERT: &str = r#"#version 300 es
uniform vec2 u_resolution;
uniform vec2 u_pan;
uniform float u_zoom;
in vec2 a_position;
void main() {
    vec2 clip = ((a_position - u_pan) * u_zoom / u_resolution) * 2.0;
    clip.y = -clip.y;
    gl_Position = vec4(clip, 0.0, 1.0);
}
"#;

pub const ROAD_FRAG: &str = r#"#version 300 es
precision mediump float;
out vec4 fragColor;
void main() {
    fragColor = vec4(30.0/255.0, 50.0/255.0, 80.0/255.0, 0.25);
}
"#;

// ── Particle trail shader ───────────────────────────────────────────

pub const PARTICLE_VERT: &str = r#"#version 300 es
uniform vec2 u_resolution;
uniform vec2 u_pan;
uniform float u_zoom;
uniform float u_brightness;
uniform float u_point_size;
in vec2 a_position;
in float a_brightness;
out float v_brightness;
void main() {
    vec2 clip = ((a_position - u_pan) * u_zoom / u_resolution) * 2.0;
    clip.y = -clip.y;
    gl_Position = vec4(clip, 0.0, 1.0);
    gl_PointSize = u_point_size;
    v_brightness = u_brightness * a_brightness;
}
"#;

pub const PARTICLE_FRAG: &str = r#"#version 300 es
precision mediump float;
uniform vec3 u_color;
in float v_brightness;
out vec4 fragColor;
void main() {
    fragColor = vec4(
        u_color.r * v_brightness,
        u_color.g * v_brightness,
        u_color.b * v_brightness,
        1.0
    );
}
"#;

// ── Fullscreen triangle (shared by blur + composite) ────────────────

pub const FULLSCREEN_VERT: &str = r#"#version 300 es
out vec2 v_uv;
void main() {
    // Generate fullscreen triangle from gl_VertexID (0,1,2)
    float x = float((gl_VertexID & 1) << 2) - 1.0;
    float y = float((gl_VertexID & 2) << 1) - 1.0;
    v_uv = vec2(x, y) * 0.5 + 0.5;
    gl_Position = vec4(x, y, 0.0, 1.0);
}
"#;

// ── Horizontal Gaussian blur ────────────────────────────────────────

pub const BLUR_H_FRAG: &str = r#"#version 300 es
precision mediump float;
uniform sampler2D u_texture;
uniform vec2 u_texel_size;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    // 9-tap Gaussian (sigma ~2.5)
    float weights[5] = float[](0.2270270, 0.1945946, 0.1216216, 0.0540540, 0.0162162);
    vec3 result = texture(u_texture, v_uv).rgb * weights[0];
    for (int i = 1; i < 5; i++) {
        float offset = float(i) * u_texel_size.x;
        result += texture(u_texture, v_uv + vec2(offset, 0.0)).rgb * weights[i];
        result += texture(u_texture, v_uv - vec2(offset, 0.0)).rgb * weights[i];
    }
    fragColor = vec4(result, 1.0);
}
"#;

// ── Vertical Gaussian blur ──────────────────────────────────────────

pub const BLUR_V_FRAG: &str = r#"#version 300 es
precision mediump float;
uniform sampler2D u_texture;
uniform vec2 u_texel_size;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    float weights[5] = float[](0.2270270, 0.1945946, 0.1216216, 0.0540540, 0.0162162);
    vec3 result = texture(u_texture, v_uv).rgb * weights[0];
    for (int i = 1; i < 5; i++) {
        float offset = float(i) * u_texel_size.y;
        result += texture(u_texture, v_uv + vec2(0.0, offset)).rgb * weights[i];
        result += texture(u_texture, v_uv - vec2(0.0, offset)).rgb * weights[i];
    }
    fragColor = vec4(result, 1.0);
}
"#;

// ── Composite (particles + bloom) ───────────────────────────────────

pub const COMPOSITE_FRAG: &str = r#"#version 300 es
precision mediump float;
uniform sampler2D u_particles;
uniform sampler2D u_bloom;
uniform float u_bloom_mix;
in vec2 v_uv;
out vec4 fragColor;

void main() {
    vec3 particles = texture(u_particles, v_uv).rgb;
    vec3 bloom = texture(u_bloom, v_uv).rgb;
    fragColor = vec4(particles + bloom * u_bloom_mix, 1.0);
}
"#;
