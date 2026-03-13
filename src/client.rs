use crate::decode;
use crate::error::{Error, Result};
use crate::pb_url;
use crate::proto_encode;
use crate::proto_response;
use crate::protobuf::ProtoMessage;
use crate::tile_coord::TileCoord;
use crate::types::{Bounds, DayOfWeek, TrafficTile};
use crate::xhr1;
use reqwest::header::{HeaderMap, RETRY_AFTER};
use std::collections::hash_map::DefaultHasher;
use std::future::Future;
use std::hash::{Hash, Hasher};
use std::time::Duration;

const MAX_CONCURRENT_TILE_REQUESTS: usize = 64;
const MAX_HTTP_RETRIES: usize = 3;
const RETRY_BASE_DELAY_MS: u64 = 150;
const RETRY_MAX_DELAY_MS: u64 = 2_000;

/// Client for fetching traffic data from Google Maps vector tiles.
///
/// No API key is required — this uses the public tile endpoint.
///
/// Supports two modes:
/// - **Live traffic**: Real-time congestion data via the `stream/pb=` endpoint
/// - **Typical traffic**: Historical average traffic via the `proto?bpb=` endpoint
pub struct TrafficClient {
    http: reqwest::Client,
}

impl TrafficClient {
    /// Create a new TrafficClient with default settings.
    pub fn new() -> Result<Self> {
        let http = reqwest::Client::builder()
            .user_agent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            .timeout(std::time::Duration::from_secs(30))
            .gzip(true)
            .build()?;

        Ok(Self { http })
    }

    /// Create a TrafficClient with a custom reqwest::Client.
    pub fn with_reqwest_client(client: reqwest::Client) -> Self {
        Self { http: client }
    }

    /// Fetch a live traffic tile and return parsed data alongside HTTP response headers.
    ///
    /// Useful for inspecting cache-control signals and diagnosing update cadence.
    pub async fn get_tile_with_headers(
        &self,
        coord: TileCoord,
    ) -> Result<(TrafficTile, Vec<(String, String)>)> {
        let url = pb_url::build_traffic_tile_url(&coord);
        let resp = self.http.get(&url).send().await?;
        let status = resp.status();
        let headers: Vec<(String, String)> = resp
            .headers()
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_str().unwrap_or("?").to_string()))
            .collect();

        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(Error::HttpStatus {
                status: status.as_u16(),
                body,
            });
        }

        let bytes = resp.bytes().await?.to_vec();
        let records = xhr1::parse_xhr1(&bytes)?;

        let mut features = Vec::new();
        let mut incidents = Vec::new();
        for record in &records {
            if record.protobuf_data.is_empty() {
                continue;
            }
            if let Ok(msg) = ProtoMessage::parse(&record.protobuf_data)
                && let Ok(tile) = decode::decode_traffic_tile(&msg, coord)
            {
                features.extend(tile.features);
                incidents.extend(tile.incidents);
            }
        }

        Ok((
            TrafficTile {
                coord,
                features,
                incidents,
            },
            headers,
        ))
    }

    /// Fetch and decode **live** traffic data for a single tile.
    ///
    /// Uses the `stream/pb=` endpoint which returns XHR1-wrapped protobuf data.
    pub async fn get_tile(&self, coord: TileCoord) -> Result<TrafficTile> {
        let url = pb_url::build_traffic_tile_url(&coord);
        let bytes = self.fetch_bytes_with_retry(&url).await?;
        let records = xhr1::parse_xhr1(&bytes)?;

        // Decode all records and merge features/incidents
        let mut features = Vec::new();
        let mut incidents = Vec::new();

        for record in &records {
            if record.protobuf_data.is_empty() {
                continue;
            }
            if let Ok(msg) = ProtoMessage::parse(&record.protobuf_data)
                && let Ok(tile) = decode::decode_traffic_tile(&msg, coord)
            {
                features.extend(tile.features);
                incidents.extend(tile.incidents);
            }
        }

        Ok(TrafficTile {
            coord,
            features,
            incidents,
        })
    }

    /// Fetch **live** traffic data for all tiles covering a bounding box.
    ///
    /// Each tile is fetched individually via the `stream/pb=` endpoint in parallel.
    pub async fn get_traffic(&self, bounds: &Bounds, zoom: u8) -> Result<Vec<TrafficTile>> {
        let coords = TileCoord::tiles_for_bounds(bounds, zoom)?;
        let http = self.http.clone();
        run_indexed_with_limit(coords.len(), MAX_CONCURRENT_TILE_REQUESTS, move |idx| {
            let coord = coords[idx];
            let http = http.clone();
            async move {
                let client = TrafficClient::with_reqwest_client(http);
                client.get_tile(coord).await
            }
        })
        .await
    }

    /// Fetch **typical** (historical average) traffic data for a set of tiles
    /// at a specific day and hour.
    ///
    /// Uses the `proto?bpb=` endpoint which accepts batched tile requests
    /// and returns all tiles in a single response.
    ///
    /// The response protobuf uses the same internal format as live traffic,
    /// just wrapped differently — no XHR1 container, but still XOR-encrypted.
    pub async fn get_typical_tiles(
        &self,
        coords: &[TileCoord],
        day: DayOfWeek,
        hour: u8,
    ) -> Result<Vec<TrafficTile>> {
        if hour > 23 {
            return Err(Error::InvalidInput(format!(
                "hour must be in 0..=23, got {hour}"
            )));
        }
        if coords.is_empty() {
            return Ok(Vec::new());
        }

        let seconds = day
            .checked_seconds_into_week(hour)
            .ok_or_else(|| Error::InvalidInput(format!("hour must be in 0..=23, got {hour}")))?;

        // Batch tiles to keep URL length under Google's limit (~100 tiles per batch)
        const MAX_TILES_PER_BATCH: usize = 100;

        if coords.len() <= MAX_TILES_PER_BATCH {
            let bpb = proto_encode::build_typical_bpb(coords, Some(seconds));
            let bpb_b64 = base64_url_encode(&bpb);
            let url = format!("https://www.google.com/maps/vt/proto?bpb={bpb_b64}");
            let bytes = self.fetch_bytes_with_retry(&url).await?;
            return proto_response::parse_proto_response(&bytes);
        }

        // Multiple batches needed — fetch sequentially to avoid rate limits
        let mut all_tiles = Vec::new();
        for chunk in coords.chunks(MAX_TILES_PER_BATCH) {
            let bpb = proto_encode::build_typical_bpb(chunk, Some(seconds));
            let bpb_b64 = base64_url_encode(&bpb);
            let url = format!("https://www.google.com/maps/vt/proto?bpb={bpb_b64}");
            let bytes = self.fetch_bytes_with_retry(&url).await?;
            let mut tiles = proto_response::parse_proto_response(&bytes)?;
            all_tiles.append(&mut tiles);
        }
        Ok(all_tiles)
    }

    /// Fetch **typical** traffic data for all tiles covering a bounding box
    /// at a specific day and hour.
    ///
    /// Unlike live traffic which fetches one tile per request, typical traffic
    /// batches all tiles into a single request to the `proto?bpb=` endpoint.
    pub async fn get_typical_traffic(
        &self,
        bounds: &Bounds,
        zoom: u8,
        day: DayOfWeek,
        hour: u8,
    ) -> Result<Vec<TrafficTile>> {
        if hour > 23 {
            return Err(Error::InvalidInput(format!(
                "hour must be in 0..=23, got {hour}"
            )));
        }
        let coords = TileCoord::tiles_for_bounds(bounds, zoom)?;
        self.get_typical_tiles(&coords, day, hour).await
    }

    /// Fetch typical traffic tiles and return the proto-level cache TTLs.
    ///
    /// Each tile in the batched response carries an `f2` field — a `fixed32`
    /// float encoding the cache TTL in seconds. This tells you how long
    /// Google considers the tile data fresh.
    pub async fn get_typical_ttls(
        &self,
        coords: &[TileCoord],
        day: DayOfWeek,
        hour: u8,
    ) -> Result<Vec<f32>> {
        let seconds = day
            .checked_seconds_into_week(hour)
            .ok_or_else(|| Error::InvalidInput(format!("hour must be in 0..=23, got {hour}")))?;
        let bpb = proto_encode::build_typical_bpb(coords, Some(seconds));
        let bpb_b64 = base64_url_encode(&bpb);
        let url = format!("https://www.google.com/maps/vt/proto?bpb={bpb_b64}");
        let bytes = self.fetch_bytes_with_retry(&url).await?;
        proto_response::parse_proto_cache_ttls(&bytes)
    }

    async fn fetch_bytes_with_retry(&self, url: &str) -> Result<Vec<u8>> {
        self.fetch_bytes_with_retry_internal(
            url,
            MAX_HTTP_RETRIES,
            RETRY_BASE_DELAY_MS,
            RETRY_MAX_DELAY_MS,
        )
        .await
    }

    async fn fetch_bytes_with_retry_internal(
        &self,
        url: &str,
        max_retries: usize,
        base_delay_ms: u64,
        max_delay_ms: u64,
    ) -> Result<Vec<u8>> {
        let seed = stable_seed(url);

        for attempt in 0..=max_retries {
            match self.http.get(url).send().await {
                Ok(response) => {
                    let status = response.status();
                    if status.is_success() {
                        let bytes = response.bytes().await?;
                        return Ok(bytes.to_vec());
                    }

                    let retry_after = retry_after_delay(response.headers());
                    let body = response.text().await.unwrap_or_default();
                    if attempt < max_retries && is_retryable_status(status.as_u16()) {
                        let delay = retry_after.unwrap_or_else(|| {
                            retry_delay_for_attempt(
                                attempt,
                                base_delay_ms,
                                max_delay_ms,
                                seed.wrapping_add(attempt as u64),
                            )
                        });
                        tokio::time::sleep(delay).await;
                        continue;
                    }

                    return Err(Error::HttpStatus {
                        status: status.as_u16(),
                        body,
                    });
                }
                Err(err) => {
                    if attempt < max_retries && is_retryable_reqwest_error(&err) {
                        let delay = retry_delay_for_attempt(
                            attempt,
                            base_delay_ms,
                            max_delay_ms,
                            seed.wrapping_add(attempt as u64),
                        );
                        tokio::time::sleep(delay).await;
                        continue;
                    }
                    return Err(err.into());
                }
            }
        }

        Err(Error::InvalidInput(
            "retry loop ended without response".to_string(),
        ))
    }
}

fn stable_seed(text: &str) -> u64 {
    let mut hasher = DefaultHasher::new();
    text.hash(&mut hasher);
    hasher.finish()
}

fn is_retryable_status(status: u16) -> bool {
    status == 408 || status == 429 || (500..=599).contains(&status)
}

fn is_retryable_reqwest_error(err: &reqwest::Error) -> bool {
    err.is_timeout() || err.is_connect() || err.is_request()
}

fn retry_after_delay(headers: &HeaderMap) -> Option<Duration> {
    let raw = headers.get(RETRY_AFTER)?.to_str().ok()?;
    let secs = raw.trim().parse::<u64>().ok()?;
    Some(Duration::from_secs(secs.min(60)))
}

fn retry_delay_for_attempt(
    attempt: usize,
    base_delay_ms: u64,
    max_delay_ms: u64,
    seed: u64,
) -> Duration {
    let shift = attempt.min(8) as u32;
    let exp = base_delay_ms.saturating_mul(1u64 << shift);
    let capped = exp.min(max_delay_ms.max(1));

    // Lightweight deterministic jitter to avoid synchronized retries.
    let jitter_span = (capped / 3).max(1);
    let jitter = seed.wrapping_mul(6364136223846793005).wrapping_add(1) % jitter_span;
    Duration::from_millis(capped + jitter)
}

async fn run_indexed_with_limit<T, Fut, F>(
    total: usize,
    max_concurrency: usize,
    mut make_future: F,
) -> Result<Vec<T>>
where
    T: Send + 'static,
    Fut: Future<Output = Result<T>> + Send + 'static,
    F: FnMut(usize) -> Fut,
{
    if total == 0 {
        return Ok(Vec::new());
    }

    let limit = max_concurrency.max(1).min(total);
    let mut set = tokio::task::JoinSet::new();
    let mut next_to_spawn = 0usize;
    let mut completed = 0usize;
    let mut ordered: Vec<Option<T>> = std::iter::repeat_with(|| None).take(total).collect();

    while next_to_spawn < limit {
        let idx = next_to_spawn;
        let fut = make_future(idx);
        set.spawn(async move { Ok::<(usize, T), Error>((idx, fut.await?)) });
        next_to_spawn += 1;
    }

    while completed < total {
        let joined = set
            .join_next()
            .await
            .ok_or_else(|| Error::InvalidInput("tile worker set ended unexpectedly".to_string()))?;
        let (idx, value) = joined??;
        if idx >= ordered.len() {
            return Err(Error::InvalidInput(format!(
                "tile worker returned out-of-range index {idx} for total {}",
                ordered.len()
            )));
        }
        if ordered[idx].is_some() {
            return Err(Error::InvalidInput(format!(
                "tile worker returned duplicate index {idx}"
            )));
        }
        ordered[idx] = Some(value);
        completed += 1;

        if next_to_spawn < total {
            let idx = next_to_spawn;
            let fut = make_future(idx);
            set.spawn(async move { Ok::<(usize, T), Error>((idx, fut.await?)) });
            next_to_spawn += 1;
        }
    }

    let mut out = Vec::with_capacity(total);
    for (idx, maybe_value) in ordered.into_iter().enumerate() {
        match maybe_value {
            Some(value) => out.push(value),
            None => {
                return Err(Error::InvalidInput(format!(
                    "missing tile result for index {idx}"
                )));
            }
        }
    }

    Ok(out)
}

/// URL-safe base64 encode (no padding).
fn base64_url_encode(data: &[u8]) -> String {
    const CHARS: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

    let mut result = Vec::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = if chunk.len() > 1 { chunk[1] as u32 } else { 0 };
        let b2 = if chunk.len() > 2 { chunk[2] as u32 } else { 0 };
        let triple = (b0 << 16) | (b1 << 8) | b2;

        result.push(CHARS[((triple >> 18) & 0x3F) as usize]);
        result.push(CHARS[((triple >> 12) & 0x3F) as usize]);
        if chunk.len() > 1 {
            result.push(CHARS[((triple >> 6) & 0x3F) as usize]);
        }
        if chunk.len() > 2 {
            result.push(CHARS[(triple & 0x3F) as usize]);
        }
    }

    String::from_utf8(result).unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    fn make_http_response(status: &str, body: &str, extra_headers: &[(&str, &str)]) -> String {
        let mut headers = format!(
            "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n",
            body.len()
        );
        for (k, v) in extra_headers {
            headers.push_str(k);
            headers.push_str(": ");
            headers.push_str(v);
            headers.push_str("\r\n");
        }
        headers.push_str("\r\n");
        headers.push_str(body);
        headers
    }

    async fn spawn_sequence_server(
        responses: Vec<String>,
    ) -> (String, Arc<AtomicUsize>, tokio::task::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind local test server");
        let addr = listener.local_addr().expect("local addr");
        let hits = Arc::new(AtomicUsize::new(0));
        let hits_for_task = hits.clone();

        let handle = tokio::spawn(async move {
            for response in responses {
                let Ok(Ok((mut socket, _))) =
                    tokio::time::timeout(Duration::from_secs(3), listener.accept()).await
                else {
                    break;
                };

                let mut request_buf = [0u8; 2048];
                let _ = tokio::time::timeout(Duration::from_secs(3), socket.read(&mut request_buf))
                    .await;

                hits_for_task.fetch_add(1, Ordering::SeqCst);
                if socket.write_all(response.as_bytes()).await.is_err() {
                    continue;
                }
                let _ = socket.flush().await;
                let _ = socket.shutdown().await;
            }
        });

        (format!("http://{addr}"), hits, handle)
    }

    #[test]
    fn test_base64_url_encode() {
        assert_eq!(base64_url_encode(b""), "");
        assert_eq!(base64_url_encode(b"f"), "Zg");
        assert_eq!(base64_url_encode(b"fo"), "Zm8");
        assert_eq!(base64_url_encode(b"foo"), "Zm9v");
        assert_eq!(base64_url_encode(b"foob"), "Zm9vYg");
        assert_eq!(base64_url_encode(b"fooba"), "Zm9vYmE");
        assert_eq!(base64_url_encode(b"foobar"), "Zm9vYmFy");

        // Check URL-safe chars (+ → -, / → _)
        let data = [0xfb, 0xff, 0xfe]; // would produce +/
        let encoded = base64_url_encode(&data);
        assert!(!encoded.contains('+'));
        assert!(!encoded.contains('/'));
    }

    #[tokio::test]
    async fn typical_hour_must_be_in_range() {
        let client = TrafficClient::new().unwrap();

        let out = client
            .get_typical_tiles(&[], DayOfWeek::Monday, 24)
            .await
            .expect_err("hour=24 should be rejected");

        assert!(matches!(out, Error::InvalidInput(_)));
    }

    #[tokio::test]
    async fn indexed_runner_preserves_order_under_parallel_completion() {
        let values = run_indexed_with_limit(20, 3, |idx| async move {
            let delay_ms = ((20 - idx) % 5) as u64;
            tokio::time::sleep(std::time::Duration::from_millis(delay_ms)).await;
            Ok(idx)
        })
        .await
        .expect("runner should succeed");

        assert_eq!(values, (0..20).collect::<Vec<_>>());
    }

    #[tokio::test]
    async fn indexed_runner_handles_empty_work() {
        let values: Vec<usize> = run_indexed_with_limit(0, 8, |_idx| async { Ok(42usize) })
            .await
            .expect("empty run should succeed");
        assert!(values.is_empty());
    }

    #[tokio::test]
    async fn indexed_runner_treats_zero_limit_as_one() {
        let values = run_indexed_with_limit(5, 0, |idx| async move { Ok(idx) })
            .await
            .expect("runner should succeed");
        assert_eq!(values, vec![0, 1, 2, 3, 4]);
    }

    #[test]
    fn retryable_status_classification() {
        assert!(is_retryable_status(408));
        assert!(is_retryable_status(429));
        assert!(is_retryable_status(500));
        assert!(is_retryable_status(503));
        assert!(!is_retryable_status(404));
        assert!(!is_retryable_status(400));
    }

    #[test]
    fn retry_after_header_parses_seconds() {
        let mut headers = HeaderMap::new();
        headers.insert(RETRY_AFTER, "17".parse().expect("header value"));
        assert_eq!(retry_after_delay(&headers), Some(Duration::from_secs(17)));
    }

    #[test]
    fn retry_delay_is_capped_and_nonzero() {
        let d1 = retry_delay_for_attempt(0, 10, 50, 1);
        let d4 = retry_delay_for_attempt(4, 10, 50, 1);
        assert!(d1 >= Duration::from_millis(10));
        assert!(d4 <= Duration::from_millis(66));
    }

    #[tokio::test]
    async fn fetch_with_retry_retries_retryable_http_status() {
        let (base_url, hits, server) = spawn_sequence_server(vec![
            make_http_response("500 Internal Server Error", "err", &[]),
            make_http_response("200 OK", "ok", &[]),
        ])
        .await;

        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .expect("reqwest client");
        let client = TrafficClient::with_reqwest_client(http);
        let bytes = client
            .fetch_bytes_with_retry_internal(&(base_url + "/x"), 2, 1, 10)
            .await
            .expect("retry should eventually succeed");

        assert_eq!(bytes, b"ok");
        server.await.expect("server task");
        assert_eq!(hits.load(Ordering::SeqCst), 2);
    }

    #[tokio::test]
    async fn fetch_with_retry_does_not_retry_non_retryable_status() {
        let (base_url, hits, server) =
            spawn_sequence_server(vec![make_http_response("404 Not Found", "missing", &[])]).await;

        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .expect("reqwest client");
        let client = TrafficClient::with_reqwest_client(http);
        let err = client
            .fetch_bytes_with_retry_internal(&(base_url + "/x"), 3, 1, 10)
            .await
            .expect_err("404 should not be retried");

        match err {
            Error::HttpStatus { status, .. } => assert_eq!(status, 404),
            other => panic!("unexpected error: {other:?}"),
        }
        server.await.expect("server task");
        assert_eq!(hits.load(Ordering::SeqCst), 1);
    }
}
