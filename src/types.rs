/// A latitude/longitude coordinate.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct LatLng {
    pub lat: f64,
    pub lng: f64,
}

impl LatLng {
    pub fn new(lat: f64, lng: f64) -> Self {
        Self { lat, lng }
    }
}

/// A geographic bounding box.
#[derive(Debug, Clone, Copy)]
pub struct Bounds {
    pub south: f64,
    pub west: f64,
    pub north: f64,
    pub east: f64,
}

impl Bounds {
    pub fn new(south: f64, west: f64, north: f64, east: f64) -> Self {
        Self {
            south,
            west,
            north,
            east,
        }
    }
}

/// Traffic congestion level for a road segment.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CongestionLevel {
    Normal,
    Slow,
    Heavy,
    Severe,
    Unknown(u8),
}

impl CongestionLevel {
    pub fn from_value(v: u8) -> Self {
        match v {
            0 => Self::Normal,
            1 => Self::Slow,
            2 => Self::Heavy,
            3 => Self::Severe,
            other => Self::Unknown(other),
        }
    }
}

/// A segment of a road feature with a congestion level.
#[derive(Debug, Clone)]
pub struct RoadSegment {
    pub start_index: usize,
    pub end_index: usize,
    pub congestion: CongestionLevel,
}

/// A traffic feature (road polyline with speed segments).
#[derive(Debug, Clone)]
pub struct TrafficFeature {
    pub geometry: Vec<LatLng>,
    pub segments: Vec<RoadSegment>,
    /// Overall bearing of the feature in degrees (0=N, 90=E, 180=S, 270=W).
    pub bearing: f64,
    pub layer_type: u32,
    pub style_flags: Option<u32>,
    pub raw_speed_data: Option<Vec<u8>>,
}

impl TrafficFeature {
    /// Compass direction of the feature (N, NE, E, SE, S, SW, W, NW).
    pub fn compass(&self) -> &'static str {
        match self.bearing {
            b if !(22.5..337.5).contains(&b) => "N",
            b if b < 67.5 => "NE",
            b if b < 112.5 => "E",
            b if b < 157.5 => "SE",
            b if b < 202.5 => "S",
            b if b < 247.5 => "SW",
            b if b < 292.5 => "W",
            _ => "NW",
        }
    }
}

/// Type of traffic incident.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum IncidentType {
    Accident,
    RoadClosure,
    Construction,
    Other(u32),
}

impl IncidentType {
    pub fn from_value(v: u32) -> Self {
        match v {
            1 => Self::Accident,
            2 => Self::RoadClosure,
            3 => Self::Construction,
            other => Self::Other(other),
        }
    }
}

/// A traffic incident.
#[derive(Debug, Clone)]
pub struct TrafficIncident {
    pub description: Option<String>,
    pub short_description: Option<String>,
    pub location: Option<LatLng>,
    pub incident_type: IncidentType,
}

/// Decoded traffic data for a single map tile.
#[derive(Debug, Clone)]
pub struct TrafficTile {
    pub coord: TileCoord,
    pub features: Vec<TrafficFeature>,
    pub incidents: Vec<TrafficIncident>,
}

use crate::tile_coord::TileCoord;

/// Day of the week for typical traffic queries.
///
/// Week starts on Sunday (= 0 seconds into the week).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DayOfWeek {
    Sunday,
    Monday,
    Tuesday,
    Wednesday,
    Thursday,
    Friday,
    Saturday,
}

impl DayOfWeek {
    /// Day index (Sunday = 0, Saturday = 6).
    pub fn index(self) -> u32 {
        match self {
            Self::Sunday => 0,
            Self::Monday => 1,
            Self::Tuesday => 2,
            Self::Wednesday => 3,
            Self::Thursday => 4,
            Self::Friday => 5,
            Self::Saturday => 6,
        }
    }

    /// Convert day + hour to `seconds_into_week` (the value Google Maps uses).
    ///
    /// `hour` is 0..=23. Sunday midnight = 0, Saturday 23:00 = 601200.
    pub fn seconds_into_week(self, hour: u8) -> u32 {
        self.index() * 86400 + hour as u32 * 3600
    }

    /// Checked variant of [`DayOfWeek::seconds_into_week`].
    ///
    /// Returns `None` when `hour` is outside `0..=23`.
    pub fn checked_seconds_into_week(self, hour: u8) -> Option<u32> {
        if hour > 23 {
            return None;
        }
        Some(self.seconds_into_week(hour))
    }
}

#[cfg(test)]
mod tests {
    use super::DayOfWeek;

    #[test]
    fn checked_seconds_into_week_rejects_invalid_hour() {
        assert_eq!(DayOfWeek::Monday.checked_seconds_into_week(24), None);
    }

    #[test]
    fn checked_seconds_into_week_matches_known_value() {
        assert_eq!(
            DayOfWeek::Friday.checked_seconds_into_week(9),
            Some(464_400)
        );
    }
}
