/// xorshift32 PRNG — fast, no dependencies, deterministic
pub struct Rng {
    state: u32,
}

impl Rng {
    pub fn new(seed: u32) -> Self {
        Self {
            state: if seed == 0 { 1 } else { seed },
        }
    }

    #[inline(always)]
    pub fn next_u32(&mut self) -> u32 {
        let mut x = self.state;
        x ^= x << 13;
        x ^= x >> 17;
        x ^= x << 5;
        self.state = x;
        x
    }

    /// Returns float in [0, 1)
    #[inline(always)]
    pub fn next_f32(&mut self) -> f32 {
        (self.next_u32() & 0x00FF_FFFF) as f32 / 16_777_216.0
    }

    /// Returns u32 in [0, max)
    #[inline(always)]
    pub fn next_range(&mut self, max: u32) -> u32 {
        ((self.next_u32() as u64 * max as u64) >> 32) as u32
    }
}

#[allow(dead_code)]
#[inline(always)]
pub fn lerp(a: f32, b: f32, t: f32) -> f32 {
    a + (b - a) * t
}

#[allow(dead_code)]
#[inline(always)]
pub fn clamp(v: f32, lo: f32, hi: f32) -> f32 {
    if v < lo {
        lo
    } else if v > hi {
        hi
    } else {
        v
    }
}

#[allow(dead_code)]
#[inline(always)]
pub fn angle_diff(a: f32, b: f32) -> f32 {
    let mut d = a - b;
    while d > std::f32::consts::PI {
        d -= 2.0 * std::f32::consts::PI;
    }
    while d < -std::f32::consts::PI {
        d += 2.0 * std::f32::consts::PI;
    }
    d.abs()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rng_produces_different_values() {
        let mut rng = Rng::new(42);
        let a = rng.next_u32();
        let b = rng.next_u32();
        assert_ne!(a, b);
    }

    #[test]
    fn rng_f32_in_range() {
        let mut rng = Rng::new(123);
        for _ in 0..1000 {
            let v = rng.next_f32();
            assert!((0.0..1.0).contains(&v));
        }
    }

    #[test]
    fn rng_range_in_bounds() {
        let mut rng = Rng::new(99);
        for _ in 0..1000 {
            let v = rng.next_range(10);
            assert!(v < 10);
        }
    }

    #[test]
    fn angle_diff_straight() {
        assert!((angle_diff(0.0, 0.0) - 0.0).abs() < 1e-6);
    }

    #[test]
    fn angle_diff_wrap() {
        let d = angle_diff(3.0, -3.0);
        assert!(d < std::f32::consts::PI + 0.01);
    }
}
