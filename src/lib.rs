mod client;
mod decode;
mod error;
mod geo_utils;
mod geometry;
#[cfg(feature = "osm")]
pub mod osm;
mod pb_url;
mod proto_encode;
mod proto_response;
mod protobuf;
mod tile_coord;
mod types;
mod xhr1;

pub use client::TrafficClient;
pub use error::Error;
pub use tile_coord::TileCoord;
pub use types::{
    Bounds, CongestionLevel, DayOfWeek, IncidentType, LatLng, RoadSegment, TrafficFeature,
    TrafficIncident, TrafficTile,
};

#[cfg(feature = "osm")]
pub use osm::{
    EdgeTravelDirection, MatchConfig, MatchOutput, MatchResult, MaxSpeedSource, Oneway, OsmEdge,
    OsmRoadNetwork, SpeedEstimationConfig, TypicalCalibrationSpec, UnmatchedSegment,
};

#[cfg(feature = "fuzzing")]
pub mod fuzz_support {
    use crate::decode;
    use crate::error::Result;
    use crate::geometry;
    #[cfg(feature = "osm")]
    use crate::osm::{self, MatchConfig, MatchOutput, OsmEdge};
    use crate::proto_response;
    use crate::protobuf::message::ProtoMessage;
    use crate::tile_coord::TileCoord;
    use crate::types::TrafficTile;
    use crate::xhr1;

    pub fn parse_protobuf(data: &[u8]) -> Result<()> {
        let _ = ProtoMessage::parse(data)?;
        Ok(())
    }

    pub fn parse_xhr1(data: &[u8]) -> Result<()> {
        let _ = xhr1::parse_xhr1(data)?;
        Ok(())
    }

    pub fn parse_proto_response(data: &[u8]) -> Result<Vec<TrafficTile>> {
        proto_response::parse_proto_response(data)
    }

    pub fn decode_geometry(data: &[u8]) -> Result<Vec<(i32, i32)>> {
        geometry::decode_tile_geometry(data)
    }

    pub fn decode_traffic_tile_from_proto(
        data: &[u8],
        zoom: u8,
        x: u32,
        y: u32,
    ) -> Result<TrafficTile> {
        let msg = ProtoMessage::parse(data)?;
        let coord = TileCoord::new(zoom, x, y)?;
        decode::decode_traffic_tile(&msg, coord)
    }

    #[cfg(feature = "osm")]
    pub fn match_osm_tile(
        edges: &[OsmEdge],
        tile: &TrafficTile,
        config: &MatchConfig,
    ) -> MatchOutput {
        osm::fuzz_match_tile_from_edges(edges, tile, config)
    }
}
