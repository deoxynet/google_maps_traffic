#!/usr/bin/env python3
"""Detect parked cars from ArcGIS Wayback satellite tiles using YOLO.

Streams tiles through YOLO in small batches — tiles are ephemeral (downloaded
to /tmp, deleted after detection). Only detection rows persist in SQLite on
the external drive.

Pipeline steps:
  1. discover     — fetch all available Wayback releases
  2. process      — stream download+detect per release (replaces old download+detect)
  3. filter       — flag detections on fast roads
  4. materialize  — DBSCAN cluster into parking_spots table
  5. all          — run 1–4 in sequence (default)

Usage:
    python scripts/detect_parked_cars.py                          # full pipeline
    python scripts/detect_parked_cars.py --step discover          # just discover releases
    python scripts/detect_parked_cars.py --step process --limit 3 # process 3 releases
    python scripts/detect_parked_cars.py --step process --bbox 33.96,34.06,-118.39,-118.26
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.spatial import KDTree
from tqdm import tqdm

BASE_DIR = Path(__file__).resolve().parent.parent
GRAPH_PATH = BASE_DIR / "web" / "graph.json"
DB_PATH = Path("/Volumes/PRO-G40/satellite_parking.db")
SYMLINK_PATH = BASE_DIR / "data" / "satellite_parking.db"
SCRATCH_DIR = Path("/tmp/sat_tiles")

ZOOM = 19
TILE_SIZE = 256

# Default bounding box: Greater LA (Long Beach to Pasadena, Malibu to Pomona)
DEFAULT_BBOX = (33.70, 34.35, -118.70, -117.75)

# Active bbox — set from CLI args or default
LAT_MIN, LAT_MAX = DEFAULT_BBOX[0], DEFAULT_BBOX[1]
LON_MIN, LON_MAX = DEFAULT_BBOX[2], DEFAULT_BBOX[3]

# Download settings
DOWNLOAD_CONCURRENCY = 20
DOWNLOAD_RETRIES = 3
DOWNLOAD_TIMEOUT = 30  # seconds

# YOLO settings — use OBB model trained on DOTA (aerial/satellite imagery)
YOLO_MODEL = "yolov8n-obb.pt"
YOLO_CONFIDENCE = 0.15
YOLO_BATCH_SIZE = 32
VEHICLE_CLASSES = {9, 10}  # DOTA: 9=large vehicle, 10=small vehicle

# Road filtering
SPEED_THRESHOLD_KPH = 35.0  # exclude detections near edges faster than this
# Speed-based buffer widths (meters from road centerline)
# Freeways are ~4-5 lanes each direction = 30-40m total width
ROAD_BUFFER_TIERS = [
    (80.0, 30.0),   # >= 80 kph (freeways): 30m buffer
    (60.0, 15.0),   # >= 60 kph (major arterials): 15m buffer
    (35.0,  6.0),   # >= 35 kph (default): 6m buffer
]

# Graph pixel <-> lat/lon transforms (from linear regression)
LON_TO_X_SCALE = 23301.6864
LON_TO_X_OFFSET = 2758399.7075
LAT_TO_Y_SCALE = -28110.8522
LAT_TO_Y_OFFSET = 957249.4208

# Wayback config URL
WAYBACK_CONFIG_URL = (
    "https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json"
)


def set_bbox(lat_min, lat_max, lon_min, lon_max):
    """Set the active bounding box (called from CLI parsing)."""
    global LAT_MIN, LAT_MAX, LON_MIN, LON_MAX
    LAT_MIN, LAT_MAX = lat_min, lat_max
    LON_MIN, LON_MAX = lon_min, lon_max


# ── Database ─────────────────────────────────────────────────────────────

def init_db(db_path):
    """Create tables if they don't exist."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS releases (
            release_id   INTEGER PRIMARY KEY,
            capture_date TEXT NOT NULL,
            item_title   TEXT,
            tile_url     TEXT NOT NULL,
            processed    INTEGER DEFAULT 0,
            tile_count   INTEGER,
            detect_count INTEGER
        );

        CREATE TABLE IF NOT EXISTS detections (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id INTEGER NOT NULL REFERENCES releases,
            lat        REAL NOT NULL,
            lon        REAL NOT NULL,
            conf       REAL NOT NULL,
            w_px       REAL,
            h_px       REAL,
            tile_x     INTEGER,
            tile_y     INTEGER,
            on_fast_road INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_det_release ON detections(release_id);
        CREATE INDEX IF NOT EXISTS idx_det_latlon ON detections(lat, lon);

        CREATE TABLE IF NOT EXISTS parking_spots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            lat             REAL NOT NULL,
            lon             REAL NOT NULL,
            times_occupied  INTEGER,
            times_empty     INTEGER,
            max_conf        REAL,
            avg_conf        REAL,
            first_seen      TEXT,
            last_seen        TEXT,
            graph_node_idx  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_spots_latlon ON parking_spots(lat, lon);
        CREATE INDEX IF NOT EXISTS idx_spots_node ON parking_spots(graph_node_idx);

        CREATE TABLE IF NOT EXISTS tile_hashes (
            tile_x       INTEGER NOT NULL,
            tile_y       INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            release_id   INTEGER NOT NULL,
            det_count    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tile_x, tile_y, content_hash)
        );

        CREATE INDEX IF NOT EXISTS idx_det_tile
            ON detections(release_id, tile_x, tile_y);
    """)
    conn.close()


def ensure_db():
    """Ensure DB exists and symlink is in place."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)

    # Create/update symlink so enrich_parking.py can find DB at data/satellite_parking.db
    SYMLINK_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SYMLINK_PATH.is_symlink() or SYMLINK_PATH.exists():
        # If it's a regular file (old DB), move it to external drive if external is empty
        if not SYMLINK_PATH.is_symlink() and SYMLINK_PATH.is_file():
            if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
                print(f"Migrating existing DB to {DB_PATH} ...")
                shutil.copy2(str(SYMLINK_PATH), str(DB_PATH))
                # Re-init in case the copy was empty
                init_db(DB_PATH)
            SYMLINK_PATH.unlink()
        elif SYMLINK_PATH.is_symlink():
            # Symlink already exists — check it points to the right place
            if SYMLINK_PATH.resolve() != DB_PATH.resolve():
                SYMLINK_PATH.unlink()
            else:
                return  # already correct
    os.symlink(str(DB_PATH), str(SYMLINK_PATH))
    print(f"Symlink: {SYMLINK_PATH} -> {DB_PATH}")


def get_db():
    ensure_db()
    return sqlite3.connect(str(DB_PATH))


# ── Tile math ────────────────────────────────────────────────────────────

def lat_to_tile_y(lat, zoom):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    return int((1 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2 * n)


def lon_to_tile_x(lon, zoom):
    n = 2 ** zoom
    return int((lon + 180) / 360 * n)


def tile_pixel_to_latlon(tile_x, tile_y, px_x, px_y, zoom):
    """Convert tile + pixel offset to lat/lon using Web Mercator."""
    n = 2 ** zoom
    global_px_x = tile_x * TILE_SIZE + px_x
    global_px_y = tile_y * TILE_SIZE + px_y
    lon = global_px_x / (n * TILE_SIZE) * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * global_px_y / (n * TILE_SIZE))))
    lat = math.degrees(lat_rad)
    return lat, lon


def latlon_to_px(lat, lon):
    """Convert lat/lon to graph pixel coords."""
    x = LON_TO_X_SCALE * lon + LON_TO_X_OFFSET
    y = LAT_TO_Y_SCALE * lat + LAT_TO_Y_OFFSET
    return x, y


def px_to_latlon(x, y):
    """Convert graph pixel coords to lat/lon."""
    lon = (x - LON_TO_X_OFFSET) / LON_TO_X_SCALE
    lat = (y - LAT_TO_Y_OFFSET) / LAT_TO_Y_SCALE
    return lat, lon


def get_tile_range():
    """Compute tile x/y ranges for our bbox at zoom level."""
    x_min = lon_to_tile_x(LON_MIN, ZOOM)
    x_max = lon_to_tile_x(LON_MAX, ZOOM)
    y_min = lat_to_tile_y(LAT_MAX, ZOOM)  # note: lat max -> y min
    y_max = lat_to_tile_y(LAT_MIN, ZOOM)
    return x_min, x_max, y_min, y_max


# ── Step 1: Discover releases ───────────────────────────────────────────

def discover_releases():
    """Fetch Wayback config and insert releases into DB."""
    import urllib.request

    print(f"Fetching {WAYBACK_CONFIG_URL} ...")
    with urllib.request.urlopen(WAYBACK_CONFIG_URL) as resp:
        config = json.loads(resp.read())

    conn = get_db()
    new_count = 0
    existing_count = 0

    for release_num, info in config.items():
        if not release_num.isdigit():
            continue

        release_id = int(release_num)
        item_title = info.get("itemTitle", "")
        tile_url = info.get("itemURL", "")

        # Extract date from itemTitle — typically "World Imagery (Wayback 2026-02-26)"
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", item_title)
        capture_date = date_match.group(1) if date_match else ""

        # Build tile URL template from itemURL
        tile_url_template = (
            "https://wayback.maptiles.arcgis.com/arcgis/rest/services/"
            f"World_Imagery/WMTS/1.0.0/default028mm/MapServer/tile/{release_id}"
            "/{z}/{y}/{x}"
        )

        existing = conn.execute(
            "SELECT release_id FROM releases WHERE release_id = ?", (release_id,)
        ).fetchone()

        if existing:
            existing_count += 1
        else:
            conn.execute(
                "INSERT INTO releases (release_id, capture_date, item_title, tile_url) "
                "VALUES (?, ?, ?, ?)",
                (release_id, capture_date, item_title, tile_url_template),
            )
            new_count += 1

    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
    processed = conn.execute("SELECT COUNT(*) FROM releases WHERE processed = 1").fetchone()[0]
    conn.close()

    print(f"New releases: {new_count}, already known: {existing_count}")
    print(f"Total in DB: {total}, processed: {processed}, remaining: {total - processed}")


# ── Step 2: Streaming process (download + detect) ───────────────────────

async def download_batch(session, sem, tile_url_template, tile_coords, scratch_dir, error_counts):
    """Download a batch of tiles to scratch dir. Returns list of (path, tx, ty) for tiles that succeeded."""
    results = []

    async def _dl(tx, ty):
        url = tile_url_template.format(z=ZOOM, y=ty, x=tx)
        dest = scratch_dir / f"{ty}_{tx}.jpg"
        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            async with sem:
                try:
                    import aiohttp
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            dest.write_bytes(data)
                            return (dest, tx, ty)
                        elif resp.status == 404:
                            return None
                        else:
                            key = f"HTTP {resp.status}"
                            error_counts[key] = error_counts.get(key, 0) + 1
                except Exception as e:
                    key = type(e).__name__
                    error_counts[key] = error_counts.get(key, 0) + 1

            if attempt < DOWNLOAD_RETRIES:
                await asyncio.sleep(1)
        return None

    tasks = [_dl(tx, ty) for tx, ty in tile_coords]
    for result in await asyncio.gather(*tasks):
        if result is not None:
            results.append(result)
    return results


def process_releases(release_id=None, limit=None):
    """Stream download+detect with hash-based dedup. Tiles are ephemeral."""
    import aiohttp
    from ultralytics import YOLO

    conn = get_db()

    if release_id:
        rows = conn.execute(
            "SELECT release_id, tile_url FROM releases WHERE release_id = ?",
            (release_id,),
        ).fetchall()
    else:
        query = "SELECT release_id, tile_url FROM releases WHERE processed = 0 ORDER BY release_id"
        if limit:
            query += f" LIMIT {limit}"
        rows = conn.execute(query).fetchall()

    if not rows:
        print("No unprocessed releases.")
        conn.close()
        return

    x_min, x_max, y_min, y_max = get_tile_range()
    total_tiles = (x_max - x_min + 1) * (y_max - y_min + 1)
    print(f"Bbox tiles: {total_tiles} ({x_max - x_min + 1} x {y_max - y_min + 1})")
    print(f"Releases to process: {len(rows)}")

    # Build full tile coord list
    all_tile_coords = [
        (tx, ty)
        for ty in range(y_min, y_max + 1)
        for tx in range(x_min, x_max + 1)
    ]
    total_batches = math.ceil(len(all_tile_coords) / YOLO_BATCH_SIZE)

    # Load hash cache from DB for dedup
    print("Loading tile hash cache...")
    hash_dict = {}  # (tile_x, tile_y, content_hash) -> (source_release_id, det_count)
    for row in conn.execute(
        "SELECT tile_x, tile_y, content_hash, release_id, det_count FROM tile_hashes"
    ):
        tx, ty, h, src_rid, dc = row
        hash_dict[(tx, ty, h)] = (src_rid, dc)
    print(f"  {len(hash_dict)} cached tile hashes")

    # Temp table for efficient bulk detection copying
    conn.execute("""
        CREATE TEMP TABLE IF NOT EXISTS _copy_tiles (
            src_rid INTEGER, tx INTEGER, ty INTEGER
        )
    """)

    model = YOLO(YOLO_MODEL)
    grand_total = 0

    for rel_idx, (rid, tile_url_template) in enumerate(rows):
        print(f"\n=== Release {rid} ({rel_idx + 1}/{len(rows)}) ===")

        # Clean scratch dir
        if SCRATCH_DIR.exists():
            shutil.rmtree(SCRATCH_DIR)
        SCRATCH_DIR.mkdir(parents=True)

        all_det_rows = []
        hash_inserts = []
        copies_needed = []  # (source_release_id, tile_x, tile_y)
        dedup_hits = 0
        new_tile_count = 0
        error_counts = {}

        pbar = tqdm(total=total_batches, desc=f"  R{rid}", unit="batch")

        # Process in batches
        for batch_idx in range(0, len(all_tile_coords), YOLO_BATCH_SIZE):
            batch_coords = all_tile_coords[batch_idx : batch_idx + YOLO_BATCH_SIZE]

            # Download batch
            async def _download():
                sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
                connector = aiohttp.TCPConnector(limit=DOWNLOAD_CONCURRENCY, keepalive_timeout=30)
                async with aiohttp.ClientSession(connector=connector) as session:
                    return await download_batch(
                        session, sem, tile_url_template, batch_coords, SCRATCH_DIR, error_counts
                    )

            tile_entries = asyncio.run(_download())

            # Hash each tile and classify as cached vs new
            new_tiles = []  # (path, tx, ty, tile_hash) — need YOLO
            for path, tx, ty in tile_entries:
                tile_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                cached = hash_dict.get((tx, ty, tile_hash))
                if cached is not None:
                    src_rid, det_count = cached
                    dedup_hits += 1
                    if det_count > 0:
                        copies_needed.append((src_rid, tx, ty))
                    # det_count == 0 → tile had no vehicles, nothing to copy
                else:
                    new_tiles.append((path, tx, ty, tile_hash))

            new_tile_count += len(new_tiles)

            # YOLO only on new (unseen) tiles
            tile_det_counts = defaultdict(int)
            if new_tiles:
                paths = [str(e[0]) for e in new_tiles]
                results = model(paths, conf=YOLO_CONFIDENCE, device="mps", verbose=False)

                for (path, tx, ty, tile_hash), result in zip(new_tiles, results):
                    obb = result.obb
                    if obb is None or len(obb) == 0:
                        continue

                    for i in range(len(obb)):
                        cls = int(obb.cls[i].item())
                        if cls not in VEHICLE_CLASSES:
                            continue

                        conf = float(obb.conf[i].item())
                        xywhr = obb.xywhr[i].tolist()
                        cx, cy = xywhr[0], xywhr[1]
                        w, h = xywhr[2], xywhr[3]

                        lat, lon = tile_pixel_to_latlon(tx, ty, cx, cy, ZOOM)
                        all_det_rows.append((
                            rid,
                            round(lat, 6),
                            round(lon, 6),
                            round(conf, 3),
                            round(w, 1),
                            round(h, 1),
                            tx,
                            ty,
                        ))
                        tile_det_counts[(tx, ty)] += 1

            # Record hashes for new tiles (including those with 0 detections)
            for path, tx, ty, tile_hash in new_tiles:
                dc = tile_det_counts.get((tx, ty), 0)
                hash_dict[(tx, ty, tile_hash)] = (rid, dc)
                hash_inserts.append((tx, ty, tile_hash, rid, dc))

            # Delete tiles from scratch
            for f in SCRATCH_DIR.iterdir():
                f.unlink()

            pbar.update(1)

        pbar.close()

        if error_counts:
            print(f"  Download errors: {error_counts}")

        # Copy detections for cached tiles via temp table JOIN
        if copies_needed:
            conn.execute("DELETE FROM _copy_tiles")
            conn.executemany("INSERT INTO _copy_tiles VALUES (?, ?, ?)", copies_needed)
            copied_rows = conn.execute("""
                SELECT d.lat, d.lon, d.conf, d.w_px, d.h_px, d.tile_x, d.tile_y
                FROM detections d
                INNER JOIN _copy_tiles c
                    ON d.release_id = c.src_rid AND d.tile_x = c.tx AND d.tile_y = c.ty
            """).fetchall()
            for row in copied_rows:
                all_det_rows.append((rid, *row))

        # Bulk insert detections + hashes, mark release processed — all atomic
        conn.executemany(
            "INSERT INTO detections (release_id, lat, lon, conf, w_px, h_px, tile_x, tile_y) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            all_det_rows,
        )
        if hash_inserts:
            conn.executemany(
                "INSERT OR IGNORE INTO tile_hashes "
                "(tile_x, tile_y, content_hash, release_id, det_count) "
                "VALUES (?, ?, ?, ?, ?)",
                hash_inserts,
            )
        conn.execute(
            "UPDATE releases SET detect_count = ?, tile_count = ?, processed = 1 "
            "WHERE release_id = ?",
            (len(all_det_rows), total_tiles, rid),
        )
        conn.commit()

        grand_total += len(all_det_rows)
        downloaded = dedup_hits + new_tile_count
        dedup_pct = dedup_hits / max(1, downloaded) * 100
        print(
            f"  Release {rid}: {len(all_det_rows)} detections "
            f"({new_tile_count} new tiles, {dedup_hits} cached [{dedup_pct:.1f}% dedup])"
        )

    # Final cleanup
    if SCRATCH_DIR.exists():
        shutil.rmtree(SCRATCH_DIR)

    conn.close()
    print(f"\nTotal detections across all releases: {grand_total}")


# ── Step 3: Road filter ──────────────────────────────────────────────────

def build_road_mask():
    """Build spatial index of fast-road segments with per-point speed for variable buffers."""
    with open(GRAPH_PATH) as f:
        graph = json.load(f)

    edges = graph["edges"]
    segments = []  # (lat, lon)
    seg_speeds = []  # speed for each point (for variable buffer)

    fast_count = 0
    for edge in edges:
        speed = edge.get("speed", 0)
        if speed <= SPEED_THRESHOLD_KPH:
            continue
        fast_count += 1

        pts = edge["pts"]
        if len(pts) < 2:
            continue

        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            lat0, lon0 = px_to_latlon(x0, y0)
            lat1, lon1 = px_to_latlon(x1, y1)

            dlat = (lat1 - lat0) * 111320
            dlon = (lon1 - lon0) * 111320 * math.cos(math.radians((lat0 + lat1) / 2))
            seg_len = math.sqrt(dlat ** 2 + dlon ** 2)

            if seg_len < 0.1:
                continue

            n_sub = max(1, int(seg_len / 5))
            for j in range(n_sub):
                t = (j + 0.5) / n_sub
                mid_lat = lat0 + t * (lat1 - lat0)
                mid_lon = lon0 + t * (lon1 - lon0)
                segments.append((mid_lat, mid_lon))
                seg_speeds.append(speed)

    print(f"Fast edges (>{SPEED_THRESHOLD_KPH} kph): {fast_count}")
    print(f"Road mask points: {len(segments)}")

    if not segments:
        return None

    seg_arr = np.array(segments)
    seg_speeds = np.array(seg_speeds)
    center_lat = (LAT_MIN + LAT_MAX) / 2
    cos_lat = math.cos(math.radians(center_lat))
    scale = np.array([111320.0, 111320.0 * cos_lat])
    seg_meters = seg_arr * scale
    tree = KDTree(seg_meters)

    return tree, scale, seg_speeds


def get_buffer_for_speed(speed):
    """Return buffer width in meters for a given road speed."""
    for threshold, buffer in ROAD_BUFFER_TIERS:
        if speed >= threshold:
            return buffer
    return ROAD_BUFFER_TIERS[-1][1]  # fallback to smallest


def filter_detections():
    """Flag detections near fast roads using speed-dependent buffer widths."""
    conn = get_db()

    # Reset all flags so we can re-filter cleanly
    conn.execute("UPDATE detections SET on_fast_road = 0")
    conn.commit()

    rows = conn.execute(
        "SELECT id, lat, lon FROM detections"
    ).fetchall()

    if not rows:
        print("No detections to filter.")
        conn.close()
        return

    print(f"Detections to check: {len(rows)}")

    result = build_road_mask()
    if result is None:
        print("Warning: no fast roads found, all detections remain on_fast_road = 0")
        conn.close()
        return

    tree, scale, seg_speeds = result

    # Print buffer tier info
    for threshold, buffer in ROAD_BUFFER_TIERS:
        count = int(np.sum(seg_speeds >= threshold))
        print(f"  Road points >= {threshold} kph: {count} (buffer={buffer}m)")

    det_ids = [r[0] for r in rows]
    det_coords = np.array([[r[1], r[2]] for r in rows])
    det_meters = det_coords * scale
    dists, indices = tree.query(det_meters)

    # Variable buffer: compare distance to the buffer for the nearest road's speed
    on_road_ids = []
    for i in range(len(dists)):
        nearest_speed = seg_speeds[indices[i]]
        buffer = get_buffer_for_speed(nearest_speed)
        if dists[i] <= buffer:
            on_road_ids.append(det_ids[i])

    if on_road_ids:
        chunk_size = 500
        for i in range(0, len(on_road_ids), chunk_size):
            chunk = on_road_ids[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"UPDATE detections SET on_fast_road = 1 WHERE id IN ({placeholders})",
                chunk,
            )
        conn.commit()

    print(f"Flagged on fast roads: {len(on_road_ids)}")
    print(f"Parked (off fast roads): {len(rows) - len(on_road_ids)}")
    conn.close()


# ── Step 4: Materialize parking spots ────────────────────────────────────

def materialize_spots():
    """Cluster detections into parking spots using DBSCAN."""
    from sklearn.cluster import DBSCAN

    conn = get_db()

    # Load all off-road detections
    rows = conn.execute(
        "SELECT id, release_id, lat, lon, conf FROM detections WHERE on_fast_road = 0"
    ).fetchall()

    if not rows:
        print("No off-road detections to cluster.")
        conn.close()
        return

    print(f"Off-road detections: {len(rows)}")

    det_ids = [r[0] for r in rows]
    release_ids = [r[1] for r in rows]
    lats = np.array([r[2] for r in rows])
    lons = np.array([r[3] for r in rows])
    confs = np.array([r[4] for r in rows])

    # Scale to meters for DBSCAN
    center_lat = (LAT_MIN + LAT_MAX) / 2
    cos_lat = math.cos(math.radians(center_lat))
    coords_m = np.column_stack([
        lats * 111320.0,
        lons * 111320.0 * cos_lat,
    ])

    # DBSCAN: eps=2m, min_samples=1 (every detection forms at least a singleton)
    print("Running DBSCAN (eps=2m)...")
    clustering = DBSCAN(eps=2.0, min_samples=1).fit(coords_m)
    labels = clustering.labels_
    n_clusters = labels.max() + 1
    print(f"Clusters (parking spots): {n_clusters}")

    # Get all processed release dates for computing times_empty
    all_releases = conn.execute(
        "SELECT release_id, capture_date FROM releases WHERE processed = 1"
    ).fetchall()
    release_dates = {rid: date for rid, date in all_releases}
    total_processed = len(all_releases)

    # Build graph node KDTree for snapping
    with open(GRAPH_PATH) as f:
        graph = json.load(f)
    nodes = graph["nodes"]
    node_xy = np.array([[n["x"], n["y"]] for n in nodes])
    node_tree = KDTree(node_xy)
    match_radius_px = 50.0

    # Vectorized aggregation using numpy (avoids O(n_clusters * n_points) Python loop)
    print("Aggregating clusters...")
    release_ids_arr = np.array(release_ids)

    # Per-cluster stats via numpy groupby pattern
    sort_idx = np.argsort(labels)
    sorted_labels = labels[sort_idx]
    sorted_lats = lats[sort_idx]
    sorted_lons = lons[sort_idx]
    sorted_confs = confs[sort_idx]
    sorted_rids = release_ids_arr[sort_idx]

    # Find cluster boundaries in sorted array
    boundaries = np.searchsorted(sorted_labels, np.arange(n_clusters + 1))

    spots = []
    for cluster_id in tqdm(range(n_clusters), desc="  Clusters", unit="k", unit_scale=1e-3):
        start, end = boundaries[cluster_id], boundaries[cluster_id + 1]
        c_lats = sorted_lats[start:end]
        c_lons = sorted_lons[start:end]
        c_confs = sorted_confs[start:end]
        c_rids = sorted_rids[start:end]

        center_lat_spot = float(c_lats.mean())
        center_lon_spot = float(c_lons.mean())
        unique_releases = set(c_rids.tolist())
        times_occupied = len(unique_releases)
        times_empty = total_processed - times_occupied

        # Get date range
        dates = sorted(release_dates.get(rid, "") for rid in unique_releases if release_dates.get(rid))
        first_seen = dates[0] if dates else ""
        last_seen = dates[-1] if dates else ""

        # Snap to nearest graph node
        px_x, px_y = latlon_to_px(center_lat_spot, center_lon_spot)
        dist, idx = node_tree.query([px_x, px_y])
        graph_node_idx = int(idx) if dist <= match_radius_px else None

        spots.append((
            round(center_lat_spot, 6),
            round(center_lon_spot, 6),
            times_occupied,
            times_empty,
            round(float(c_confs.max()), 3),
            round(float(c_confs.mean()), 3),
            first_seen,
            last_seen,
            graph_node_idx,
        ))

    # Full rebuild of parking_spots
    conn.execute("DELETE FROM parking_spots")
    conn.executemany(
        "INSERT INTO parking_spots "
        "(lat, lon, times_occupied, times_empty, max_conf, avg_conf, first_seen, last_seen, graph_node_idx) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        spots,
    )
    conn.commit()
    conn.close()

    print(f"Materialized {len(spots)} parking spots")
    matched = sum(1 for s in spots if s[8] is not None)
    print(f"  Snapped to graph nodes: {matched}/{len(spots)}")

    # Print top spots
    top = sorted(spots, key=lambda s: s[2], reverse=True)[:10]
    if top:
        print(f"  Top spots by occupancy:")
        for s in top:
            print(f"    ({s[0]:.5f}, {s[1]:.5f}) occupied={s[2]} empty={s[3]} conf={s[5]:.2f}")


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Detect parked cars from satellite imagery")
    parser.add_argument(
        "--step",
        choices=["discover", "process", "filter", "materialize", "all"],
        default="all",
        help="Pipeline step to run (default: all)",
    )
    parser.add_argument(
        "--release",
        type=int,
        default=None,
        help="Process only this release ID",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of releases to process",
    )
    parser.add_argument(
        "--bbox",
        type=str,
        default=None,
        help="Bounding box as lat_min,lat_max,lon_min,lon_max (default: Greater LA)",
    )
    args = parser.parse_args()

    # Parse bbox
    if args.bbox:
        parts = [float(x.strip()) for x in args.bbox.split(",")]
        if len(parts) != 4:
            parser.error("--bbox must have exactly 4 comma-separated values")
        set_bbox(*parts)

    x_min, x_max, y_min, y_max = get_tile_range()
    total_tiles = (x_max - x_min + 1) * (y_max - y_min + 1)
    print(f"Bbox: ({LAT_MIN}, {LAT_MAX}) x ({LON_MIN}, {LON_MAX})")
    print(f"Tiles at z{ZOOM}: {total_tiles:,}")
    print(f"DB: {DB_PATH}")

    # Ensure DB exists
    get_db().close()

    if args.step in ("discover", "all"):
        print("\n=== Step 1: Discover releases ===")
        discover_releases()

    if args.step in ("process", "all"):
        print("\n=== Step 2: Stream process (download + detect) ===")
        process_releases(release_id=args.release, limit=args.limit)

    if args.step in ("filter", "all"):
        print("\n=== Step 3: Road filter ===")
        filter_detections()

    if args.step in ("materialize", "all"):
        print("\n=== Step 4: Materialize parking spots ===")
        materialize_spots()


if __name__ == "__main__":
    main()
