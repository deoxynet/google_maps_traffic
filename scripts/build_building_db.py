#!/usr/bin/env python3
"""Convert osm_buildings.jsonl → SQLite with R-tree index for fast spatial queries."""
import json
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = BASE_DIR / "data" / "osm_buildings.jsonl"
DB_PATH = BASE_DIR / "data" / "osm_buildings.db"

BATCH_SIZE = 50_000


def main():
    input_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    if not input_path.exists():
        print(f"Error: {input_path} not found. Run extract_osm_features first.")
        sys.exit(1)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript("""
        CREATE TABLE buildings (
            id INTEGER PRIMARY KEY,
            min_lat REAL NOT NULL,
            max_lat REAL NOT NULL,
            min_lon REAL NOT NULL,
            max_lon REAL NOT NULL
        );
        CREATE TABLE building_coords (
            building_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            PRIMARY KEY (building_id, seq)
        ) WITHOUT ROWID;
        CREATE VIRTUAL TABLE buildings_rtree USING rtree(
            id, min_lat, max_lat, min_lon, max_lon
        );
    """)

    building_rows = []
    coord_rows = []
    rtree_rows = []
    bid = 0

    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            coords = json.loads(line)
            if len(coords) < 3:
                continue

            lats = [c[0] for c in coords]
            lons = [c[1] for c in coords]
            min_lat, max_lat = min(lats), max(lats)
            min_lon, max_lon = min(lons), max(lons)

            building_rows.append((bid, min_lat, max_lat, min_lon, max_lon))
            rtree_rows.append((bid, min_lat, max_lat, min_lon, max_lon))
            for seq, (lat, lon) in enumerate(coords):
                coord_rows.append((bid, seq, lat, lon))

            bid += 1
            if bid % BATCH_SIZE == 0:
                _flush(conn, building_rows, coord_rows, rtree_rows)
                building_rows.clear()
                coord_rows.clear()
                rtree_rows.clear()
                print(f"  {bid} buildings indexed...")

    if building_rows:
        _flush(conn, building_rows, coord_rows, rtree_rows)

    conn.execute("PRAGMA synchronous=FULL")
    conn.close()
    print(f"Done: {bid} buildings → {DB_PATH}")


def _flush(conn, building_rows, coord_rows, rtree_rows):
    conn.executemany("INSERT INTO buildings VALUES (?,?,?,?,?)", building_rows)
    conn.executemany("INSERT INTO building_coords VALUES (?,?,?,?)", coord_rows)
    conn.executemany("INSERT INTO buildings_rtree VALUES (?,?,?,?,?)", rtree_rows)
    conn.commit()


if __name__ == "__main__":
    main()
