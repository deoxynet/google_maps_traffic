#!/usr/bin/env python3
"""Convert osm_roads.jsonl → SQLite with R-tree index for fast spatial queries."""
import json
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = BASE_DIR / "data" / "osm_roads.jsonl"
DB_PATH = BASE_DIR / "data" / "osm_roads.db"

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
        CREATE TABLE road_segments (
            id INTEGER PRIMARY KEY,
            start_lat REAL NOT NULL,
            start_lon REAL NOT NULL,
            end_lat REAL NOT NULL,
            end_lon REAL NOT NULL,
            lanes INTEGER NOT NULL,
            highway TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE roads_rtree USING rtree(
            id, min_lat, max_lat, min_lon, max_lon
        );
    """)

    seg_rows = []
    rtree_rows = []
    sid = 0

    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            seg = json.loads(line)
            s_lat, s_lon = seg["s"]
            e_lat, e_lon = seg["e"]
            lanes = seg["l"]
            highway = seg["h"]

            min_lat = min(s_lat, e_lat)
            max_lat = max(s_lat, e_lat)
            min_lon = min(s_lon, e_lon)
            max_lon = max(s_lon, e_lon)

            seg_rows.append((sid, s_lat, s_lon, e_lat, e_lon, lanes, highway))
            rtree_rows.append((sid, min_lat, max_lat, min_lon, max_lon))

            sid += 1
            if sid % BATCH_SIZE == 0:
                _flush(conn, seg_rows, rtree_rows)
                seg_rows.clear()
                rtree_rows.clear()
                print(f"  {sid} road segments indexed...")

    if seg_rows:
        _flush(conn, seg_rows, rtree_rows)

    conn.execute("PRAGMA synchronous=FULL")
    conn.close()
    print(f"Done: {sid} road segments → {DB_PATH}")


def _flush(conn, seg_rows, rtree_rows):
    conn.executemany(
        "INSERT INTO road_segments VALUES (?,?,?,?,?,?,?)", seg_rows
    )
    conn.executemany("INSERT INTO roads_rtree VALUES (?,?,?,?,?)", rtree_rows)
    conn.commit()


if __name__ == "__main__":
    main()
