"""Per-tile building mask for filtering rooftop false-positive detections.

Public API:
    get_building_mask(tx, ty) → 256x256 numpy bool array
    is_on_building(tx, ty, cx, cy) → bool
"""
import math
import sqlite3
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "osm_buildings.db"

_conn = None


def _get_conn():
    global _conn
    if _conn is None:
        if not DB_PATH.exists():
            raise FileNotFoundError(f"Building DB not found: {DB_PATH}")
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    return _conn


def _tile_bbox(tx, ty, zoom=19):
    """Return (min_lat, max_lat, min_lon, max_lon) for a Web Mercator tile."""
    n = 2.0 ** zoom
    # lon
    min_lon = tx / n * 360.0 - 180.0
    max_lon = (tx + 1) / n * 360.0 - 180.0
    # lat (top-left = max_lat, bottom-right = min_lat)
    max_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))
    min_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))
    return min_lat, max_lat, min_lon, max_lon


def _latlon_to_tile_px(lat, lon, min_lat, max_lat, min_lon, max_lon, size=256):
    """Convert lat/lon to pixel coords within a tile."""
    x = (lon - min_lon) / (max_lon - min_lon) * size
    y = (max_lat - lat) / (max_lat - min_lat) * size
    return x, y


@lru_cache(maxsize=2000)
def get_building_mask(tx, ty):
    """Return a 256x256 numpy bool array where True = building footprint."""
    conn = _get_conn()
    min_lat, max_lat, min_lon, max_lon = _tile_bbox(tx, ty)

    # R-tree query: buildings overlapping tile bbox
    rows = conn.execute(
        "SELECT id FROM buildings_rtree "
        "WHERE max_lat >= ? AND min_lat <= ? AND max_lon >= ? AND min_lon <= ?",
        (min_lat, max_lat, min_lon, max_lon),
    ).fetchall()

    if not rows:
        return np.zeros((256, 256), dtype=bool)

    img = Image.new("1", (256, 256), 0)
    draw = ImageDraw.Draw(img)

    for (bid,) in rows:
        coords = conn.execute(
            "SELECT lat, lon FROM building_coords WHERE building_id=? ORDER BY seq",
            (bid,),
        ).fetchall()
        if len(coords) < 3:
            continue
        poly = [
            _latlon_to_tile_px(lat, lon, min_lat, max_lat, min_lon, max_lon)
            for lat, lon in coords
        ]
        draw.polygon(poly, fill=1)

    return np.array(img, dtype=bool)


def is_on_building(tx, ty, cx, cy):
    """Check if pixel (cx, cy) falls on a building in tile (tx, ty)."""
    mask = get_building_mask(tx, ty)
    ix, iy = int(cx), int(cy)
    if 0 <= ix < 256 and 0 <= iy < 256:
        return bool(mask[iy, ix])
    return False
