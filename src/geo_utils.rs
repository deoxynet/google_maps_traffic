use crate::types::LatLng;

#[cfg(any(test, feature = "osm"))]
const EARTH_RADIUS_M: f64 = 6_371_000.0;

#[cfg(any(test, feature = "osm"))]
#[inline]
pub(crate) fn wrap_lon(lon: f64) -> f64 {
    if !lon.is_finite() {
        return lon;
    }
    let wrapped = (lon + 180.0).rem_euclid(360.0) - 180.0;
    if wrapped == -180.0 && lon > 0.0 {
        180.0
    } else {
        wrapped
    }
}

#[inline]
pub(crate) fn shortest_lng_delta_deg(delta: f64) -> f64 {
    if !delta.is_finite() {
        return delta;
    }
    let wrapped = (delta + 180.0).rem_euclid(360.0) - 180.0;
    if wrapped == -180.0 { 180.0 } else { wrapped }
}

/// Compute initial bearing in degrees (0..360) from first to last point.
pub(crate) fn compute_bearing(geom: &[LatLng]) -> f64 {
    if geom.len() < 2 {
        return 0.0;
    }

    let start = geom[0];
    let end = geom[geom.len() - 1];

    let lat1 = start.lat.to_radians();
    let lat2 = end.lat.to_radians();
    let dlng = shortest_lng_delta_deg(end.lng - start.lng).to_radians();

    let bearing_rad = (dlng.sin() * lat2.cos())
        .atan2(lat1.cos() * lat2.sin() - lat1.sin() * lat2.cos() * dlng.cos());
    (bearing_rad.to_degrees() + 360.0) % 360.0
}

#[cfg(any(test, feature = "osm"))]
pub(crate) fn haversine_distance_m(a: LatLng, b: LatLng) -> f64 {
    let dlat = (b.lat - a.lat).to_radians();
    let dlng = shortest_lng_delta_deg(b.lng - a.lng).to_radians();
    let lat1 = a.lat.to_radians();
    let lat2 = b.lat.to_radians();

    let h = (dlat / 2.0).sin().powi(2) + lat1.cos() * lat2.cos() * (dlng / 2.0).sin().powi(2);

    2.0 * EARTH_RADIUS_M * h.sqrt().asin()
}

#[cfg(any(test, feature = "osm"))]
/// Small-angle 2D projection for short-segment distance checks.
pub(crate) fn point_to_segment_distance_m(point: LatLng, start: LatLng, end: LatLng) -> f64 {
    let mean_lat_rad = ((start.lat + end.lat + point.lat) / 3.0).to_radians();
    let cos_lat = mean_lat_rad.cos().abs().max(1e-12);

    let vx = shortest_lng_delta_deg(end.lng - start.lng) * cos_lat;
    let vy = end.lat - start.lat;
    let px = shortest_lng_delta_deg(point.lng - start.lng) * cos_lat;
    let py = point.lat - start.lat;
    let len_sq = vx * vx + vy * vy;

    if len_sq <= f64::EPSILON {
        return haversine_distance_m(point, start);
    }

    let t = ((px * vx + py * vy) / len_sq).clamp(0.0, 1.0);
    let proj = LatLng {
        lat: start.lat + t * vy,
        lng: wrap_lon(start.lng + (t * vx) / cos_lat),
    };

    haversine_distance_m(point, proj)
}

#[cfg(any(test, feature = "osm"))]
pub(crate) fn bearing_diff_deg(a: f64, b: f64) -> f64 {
    let diff = (a - b).abs() % 360.0;
    if diff > 180.0 { 360.0 - diff } else { diff }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bearing_cardinal_cases() {
        let north = [LatLng::new(34.0, -118.0), LatLng::new(35.0, -118.0)];
        let east = [LatLng::new(34.0, -118.0), LatLng::new(34.0, -117.0)];

        assert!(compute_bearing(&north) < 1.0);
        assert!((compute_bearing(&east) - 90.0).abs() < 1.0);
    }

    #[test]
    fn haversine_one_degree_lat() {
        let a = LatLng::new(0.0, 0.0);
        let b = LatLng::new(1.0, 0.0);
        let d = haversine_distance_m(a, b);
        assert!((d - 111_195.0).abs() < 300.0);
    }

    #[test]
    fn distance_to_segment_midpoint_projection() {
        let start = LatLng::new(34.0, -118.0);
        let end = LatLng::new(34.0, -117.99);
        let point = LatLng::new(34.001, -117.995);

        let d = point_to_segment_distance_m(point, start, end);
        assert!((d - 111.0).abs() < 15.0);
    }

    #[test]
    fn distance_to_segment_clamps_to_endpoint() {
        let start = LatLng::new(0.0, 0.0);
        let end = LatLng::new(0.0, 0.01);
        let point = LatLng::new(0.0, 0.02);

        let d = point_to_segment_distance_m(point, start, end);
        assert!((d - 1_111.95).abs() < 30.0);
    }

    #[test]
    fn bearing_diff_wraparound() {
        assert!((bearing_diff_deg(350.0, 10.0) - 20.0).abs() < 1e-9);
        assert!((bearing_diff_deg(10.0, 350.0) - 20.0).abs() < 1e-9);
    }

    #[test]
    fn distance_to_segment_handles_antimeridian_crossing() {
        let start = LatLng::new(0.0, 179.9);
        let end = LatLng::new(0.0, -179.9);
        let point = LatLng::new(0.0, 180.0);

        let d = point_to_segment_distance_m(point, start, end);
        assert!(
            d < 20.0,
            "distance should be near-zero across dateline, got {d}"
        );
    }

    #[test]
    fn bearing_handles_antimeridian_crossing() {
        let edge = [LatLng::new(0.0, 179.9), LatLng::new(0.0, -179.9)];
        let b = compute_bearing(&edge);
        assert!(
            (b - 90.0).abs() < 1.0,
            "expected eastbound bearing, got {b}"
        );
    }
}
