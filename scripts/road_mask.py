"""Per-tile road zone mask for classifying parked vs driving vehicles.

Rasterizes OSM road centerlines into a 256x256 uint8 mask per tile:
    0 = off-road
    1 = parking edge (outer ~2.5m on each side)
    2 = driving center

Public API:
    get_road_mask(tx, ty) → 256x256 numpy uint8 array
    road_zone(tx, ty, cx, cy) → int (0, 1, or 2)
"""
import math
import sqlite3
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "osm_roads.db"

_conn = None

# At z19, lat ~34: ~0.247 m/px
_METERS_PER_PX = 0.247
_LANE_WIDTH_M = 3.5
_PARKING_LANE_M = 2.5
_CURB_MARGIN_M = 0.5


def _get_conn():
    global _conn
    if _conn is None:
        if not DB_PATH.exists():
            raise FileNotFoundError(f"Road DB not found: {DB_PATH}")
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    return _conn


def _tile_bbox(tx, ty, zoom=19):
    """Return (min_lat, max_lat, min_lon, max_lon) for a Web Mercator tile."""
    n = 2.0 ** zoom
    min_lon = tx / n * 360.0 - 180.0
    max_lon = (tx + 1) / n * 360.0 - 180.0
    max_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    min_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
    return min_lat, max_lat, min_lon, max_lon


def _latlon_to_tile_px(lat, lon, min_lat, max_lat, min_lon, max_lon, size=256):
    x = (lon - min_lon) / (max_lon - min_lon) * size
    y = (max_lat - lat) / (max_lat - min_lat) * size
    return x, y


def _road_width_px(lanes):
    """Full road width in pixels from lane count."""
    width_m = lanes * _LANE_WIDTH_M + 2 * _CURB_MARGIN_M
    return max(1, int(width_m / _METERS_PER_PX))


def _drive_width_px(lanes):
    """Inner driving zone width in pixels (excludes parking edges)."""
    if lanes <= 1:
        # Single-lane roads: no parking zone distinction
        return _road_width_px(lanes)
    width_m = lanes * _LANE_WIDTH_M + 2 * _CURB_MARGIN_M - 2 * _PARKING_LANE_M
    return max(1, int(width_m / _METERS_PER_PX))


@lru_cache(maxsize=2000)
def get_road_mask(tx, ty):
    """Return a 256x256 uint8 array: 0=off-road, 1=parking-edge, 2=driving."""
    conn = _get_conn()
    min_lat, max_lat, min_lon, max_lon = _tile_bbox(tx, ty)

    # Pad the query bbox slightly to catch roads whose width extends into tile
    pad = 0.0003  # ~30m
    rows = conn.execute(
        "SELECT id FROM roads_rtree "
        "WHERE max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ?",
        (min_lat - pad, max_lat + pad, min_lon - pad, max_lon + pad),
    ).fetchall()

    if not rows:
        return np.zeros((256, 256), dtype=np.uint8)

    # Draw full road width first, then overdraw driving zone
    road_img = Image.new("L", (256, 256), 0)
    drive_img = Image.new("L", (256, 256), 0)
    road_draw = ImageDraw.Draw(road_img)
    drive_draw = ImageDraw.Draw(drive_img)

    ids = [r[0] for r in rows]
    # Batch fetch segments
    placeholders = ",".join("?" * len(ids))
    segs = conn.execute(
        f"SELECT start_lat, start_lon, end_lat, end_lon, lanes "
        f"FROM road_segments WHERE id IN ({placeholders})",
        ids,
    ).fetchall()

    for s_lat, s_lon, e_lat, e_lon, lanes in segs:
        sx, sy = _latlon_to_tile_px(s_lat, s_lon, min_lat, max_lat, min_lon, max_lon)
        ex, ey = _latlon_to_tile_px(e_lat, e_lon, min_lat, max_lat, min_lon, max_lon)

        rw = _road_width_px(lanes)
        dw = _drive_width_px(lanes)

        road_draw.line([(sx, sy), (ex, ey)], fill=255, width=rw)
        drive_draw.line([(sx, sy), (ex, ey)], fill=255, width=dw)

    road_arr = np.array(road_img)
    drive_arr = np.array(drive_img)

    # Compose: 2=driving (inner), 1=parking (road minus driving), 0=off-road
    mask = np.zeros((256, 256), dtype=np.uint8)
    mask[road_arr > 0] = 1   # parking edge
    mask[drive_arr > 0] = 2  # driving center
    return mask


def road_zone(tx, ty, cx, cy):
    """Return road zone for pixel (cx, cy): 0=off-road, 1=parking, 2=driving."""
    mask = get_road_mask(tx, ty)
    ix, iy = int(cx), int(cy)
    if 0 <= ix < 256 and 0 <= iy < 256:
        return int(mask[iy, ix])
    return 0
