use crate::tile_coord::TileCoord;

/// Build the Google Maps traffic tile URL for a given tile coordinate.
///
/// This constructs the `pb=` encoded URL for the stream endpoint that returns
/// traffic vector tile data with live congestion and incident information.
///
/// The URL format is derived from observed Google Maps traffic layer requests.
/// The `spotlit` + `traffic` layers provide road congestion data and incidents.
pub fn build_traffic_tile_url(coord: &TileCoord) -> String {
    // The `!3x` value is a layer bitmask; 1073345 requests traffic fill/flow data.
    // The `!6m31` section contains render parameters matching real Google Maps requests.
    format!(
        "https://www.google.com/maps/vt/stream/pb=\
         !1m7!8m6!1m3!1i{zoom}!2i{x}!3i{y}!2i7!3x1073345\
         !2m2!1e2!2sspotlit\
         !2m8!1e2!2straffic!4m2!1sincidents!2s1!4m2!1sincidents_text!2s1\
         !3m3!2sen!3sus!5e1105\
         !4e1\
         !5m4!1e4!8m2!1e0!1e1\
         !6m31\
         !1e12!2i2!6b0!7b1\
         !19m1!1e0!20m1!1e0\
         !28e6!30m1!1f4!32b1\
         !39b1!40b1!41e1!43b1!44e1!45b1\
         !50e0!57b1!60b1\
         !62i6!62i9!62i12!62i15\
         !63i8!63i11!63i14!63i21\
         !67m1!1e1",
        zoom = coord.zoom,
        x = coord.x,
        y = coord.y,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_url_contains_coords() {
        let coord = TileCoord::new(12, 1205, 1540).unwrap();
        let url = build_traffic_tile_url(&coord);
        assert!(url.contains("!1i12"));
        assert!(url.contains("!2i1205"));
        assert!(url.contains("!3i1540"));
        assert!(url.starts_with("https://www.google.com/maps/vt/stream/pb="));
    }

    #[test]
    fn test_url_has_traffic_layers() {
        let coord = TileCoord::new(14, 2810, 6548).unwrap();
        let url = build_traffic_tile_url(&coord);
        assert!(url.contains("!2sspotlit"));
        assert!(url.contains("!2straffic"));
        assert!(url.contains("!1sincidents"));
    }
}
