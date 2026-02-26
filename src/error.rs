#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("invalid input: {0}")]
    InvalidInput(String),

    #[error("invalid tile coordinate: {0}")]
    InvalidTileCoord(String),

    #[error("invalid zoom level {0} (must be 0..=21)")]
    InvalidZoom(u8),

    #[error("XHR1 parse error: {0}")]
    Xhr1Parse(String),

    #[error("missing XHR1 magic bytes")]
    Xhr1MissingMagic,

    #[error("protobuf decode error: {0}")]
    ProtobufDecode(String),

    #[error("varint overflow")]
    VarintOverflow,

    #[error("truncated data: expected {expected} bytes, got {actual}")]
    Truncated { expected: usize, actual: usize },

    #[error("geometry decode error: {0}")]
    GeometryDecode(String),

    #[error("HTTP request failed: {0}")]
    Reqwest(#[from] reqwest::Error),

    #[error("task join error: {0}")]
    TaskJoin(#[from] tokio::task::JoinError),

    #[error("HTTP {status}: {body}")]
    HttpStatus { status: u16, body: String },
}

pub type Result<T> = std::result::Result<T, Error>;
