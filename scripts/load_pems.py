#!/usr/bin/env python3
"""
Load PeMS station metadata and 5-minute flow data into SQLite.

Aggregates 5-min intervals to hourly totals. Stores all station types
but the primary use case is ramp detectors (OR/FR) for freeway entry/exit volumes.

Usage:
    python scripts/load_pems.py
    python scripts/load_pems.py --data-dir pems_data --db data/pems.db
"""

import argparse
import csv
import gzip
import sqlite3
from collections import defaultdict
from pathlib import Path

from tqdm import tqdm

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = BASE_DIR / "pems_data"
DEFAULT_DB = BASE_DIR / "data" / "pems.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    id INTEGER PRIMARY KEY,
    freeway INTEGER,
    direction TEXT,
    district INTEGER,
    county INTEGER,
    city INTEGER,
    state_pm TEXT,
    abs_pm REAL,
    lat REAL,
    lon REAL,
    length REAL,
    type TEXT,
    lanes INTEGER,
    name TEXT
);

CREATE TABLE IF NOT EXISTS hourly_flow (
    station_id INTEGER REFERENCES stations,
    date TEXT,
    hour INTEGER,
    type TEXT,
    freeway INTEGER,
    direction TEXT,
    flow INTEGER,
    avg_occupancy REAL,
    avg_speed REAL,
    samples INTEGER,
    pct_observed REAL,
    PRIMARY KEY (station_id, date, hour)
);

CREATE INDEX IF NOT EXISTS idx_hourly_type ON hourly_flow(type);
CREATE INDEX IF NOT EXISTS idx_hourly_freeway ON hourly_flow(freeway, direction, hour);
"""


def load_metadata(conn, data_dir):
    """Load all metadata TSV files."""
    meta_files = sorted(data_dir.glob("d*_text_meta_*.txt"))
    if not meta_files:
        print("No metadata files found!")
        return

    total = 0
    for f in meta_files:
        district = f.name.split("_")[0]  # e.g. "d07"
        print(f"  Loading {f.name}...")
        with open(f) as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            rows = []
            for row in reader:
                try:
                    rows.append((
                        int(row["ID"]),
                        int(row["Fwy"]) if row.get("Fwy") else None,
                        row.get("Dir"),
                        int(row["District"]) if row.get("District") else None,
                        int(row["County"]) if row.get("County") else None,
                        int(row["City"]) if row.get("City") else None,
                        row.get("State_PM"),
                        float(row["Abs_PM"]) if row.get("Abs_PM") else None,
                        float(row["Latitude"]) if row.get("Latitude") else None,
                        float(row["Longitude"]) if row.get("Longitude") else None,
                        float(row["Length"]) if row.get("Length") else None,
                        row.get("Type"),
                        int(row["Lanes"]) if row.get("Lanes") else None,
                        row.get("Name"),
                    ))
                except (ValueError, KeyError):
                    continue
            conn.executemany(
                "INSERT OR REPLACE INTO stations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            total += len(rows)

    conn.commit()
    print(f"  Loaded {total:,} stations")


def load_5min_data(conn, data_dir):
    """Load 5-min gzipped data files, aggregate to hourly, insert."""
    data_files = sorted(data_dir.glob("d*_text_station_5min_*.txt.gz"))
    if not data_files:
        print("No 5-min data files found!")
        return

    # Build station lookup for type/freeway/direction from DB
    station_info = {}
    for row in conn.execute("SELECT id, type, freeway, direction FROM stations"):
        station_info[row[0]] = (row[1], row[2], row[3])

    for f in data_files:
        district = f.name.split("_")[0]
        print(f"  Loading {f.name}...")

        # Accumulate per (station, date, hour)
        hourly = defaultdict(lambda: {
            "flow": 0, "occ_sum": 0.0, "speed_sum": 0.0,
            "speed_flow": 0.0, "samples": 0, "obs_sum": 0.0, "n": 0,
        })

        with gzip.open(f, "rt") as fh:
            line_count = 0
            for line in tqdm(fh, desc=f"  {district}", unit=" rows", unit_scale=True):
                line_count += 1
                parts = line.strip().split(",")
                if len(parts) < 12:
                    continue

                timestamp = parts[0]      # MM/DD/YYYY HH:MI:SS
                try:
                    station_id = int(parts[1])
                except ValueError:
                    continue

                date_part = timestamp[:10]  # MM/DD/YYYY
                hour = int(timestamp[11:13])

                # Parse aggregate fields
                try:
                    flow = int(parts[9]) if parts[9] else 0
                except ValueError:
                    flow = 0
                try:
                    occ = float(parts[10]) if parts[10] else 0.0
                except ValueError:
                    occ = 0.0
                try:
                    speed = float(parts[11]) if parts[11] else 0.0
                except ValueError:
                    speed = 0.0
                try:
                    samples = int(parts[7]) if parts[7] else 0
                except ValueError:
                    samples = 0
                try:
                    pct_obs = float(parts[8]) if parts[8] else 0.0
                except ValueError:
                    pct_obs = 0.0

                key = (station_id, date_part, hour)
                h = hourly[key]
                h["flow"] += flow
                h["occ_sum"] += occ
                h["speed_flow"] += speed * flow
                h["samples"] += samples
                h["obs_sum"] += pct_obs
                h["n"] += 1

        # Convert date format and insert
        rows = []
        for (station_id, date_str, hour), h in hourly.items():
            # MM/DD/YYYY -> YYYY-MM-DD
            iso_date = f"{date_str[6:10]}-{date_str[0:2]}-{date_str[3:5]}"
            info = station_info.get(station_id, (None, None, None))
            avg_occ = h["occ_sum"] / h["n"] if h["n"] > 0 else None
            avg_speed = h["speed_flow"] / h["flow"] if h["flow"] > 0 else None
            avg_obs = h["obs_sum"] / h["n"] if h["n"] > 0 else None

            rows.append((
                station_id, iso_date, hour,
                info[0], info[1], info[2],
                h["flow"], avg_occ, avg_speed,
                h["samples"], avg_obs,
            ))

        conn.executemany(
            "INSERT OR REPLACE INTO hourly_flow VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        print(f"    {len(rows):,} hourly records from {line_count:,} 5-min rows")


def print_stats(conn):
    print("\n=== PeMS Database Stats ===")
    total = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
    print(f"  Stations: {total:,}")
    for row in conn.execute(
        "SELECT type, COUNT(*) FROM stations GROUP BY type ORDER BY COUNT(*) DESC"
    ):
        print(f"    {row[0]:4s} {row[1]:,}")

    print()
    total = conn.execute("SELECT COUNT(*) FROM hourly_flow").fetchone()[0]
    print(f"  Hourly flow records: {total:,}")
    ramp = conn.execute(
        "SELECT COUNT(*) FROM hourly_flow WHERE type IN ('OR','FR')"
    ).fetchone()[0]
    print(f"  Ramp records (OR+FR): {ramp:,}")

    print()
    print("  Sample ramp data (busiest on-ramps at 8 AM):")
    for row in conn.execute("""
        SELECT s.name, s.freeway, s.direction, h.flow, h.avg_speed
        FROM hourly_flow h
        JOIN stations s ON s.id = h.station_id
        WHERE h.type = 'OR' AND h.hour = 8
        ORDER BY h.flow DESC
        LIMIT 10
    """):
        name = row[0] or "?"
        print(f"    {name:25s} Fwy {row[1]:3d}-{row[2]}  {row[3]:5,} veh  {row[4]:4.0f} mph" if row[4] else
              f"    {name:25s} Fwy {row[1]:3d}-{row[2]}  {row[3]:5,} veh")


def main():
    parser = argparse.ArgumentParser(description="Load PeMS data into SQLite")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")

    print("Loading station metadata...")
    load_metadata(conn, args.data_dir)

    print("\nLoading 5-min data (aggregating to hourly)...")
    load_5min_data(conn, args.data_dir)

    print_stats(conn)
    conn.close()


if __name__ == "__main__":
    main()
