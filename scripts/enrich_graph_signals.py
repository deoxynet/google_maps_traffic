#!/usr/bin/env python3
"""Enrich web/graph.json nodes with signal timing data from navigatela.db.

For each graph node with light=true, spatially matches to the nearest
intersection in the DB (within 50m) and extracts per-period signal timing.

Adds a `signal` field: [AM_cycle, AM_ns_green, PM_cycle, PM_ns_green,
                          MID_cycle, MID_ns_green, OFF_cycle, OFF_ns_green]
"""

import json
import sqlite3
from pathlib import Path

import numpy as np
from scipy.spatial import KDTree

BASE_DIR = Path(__file__).resolve().parent.parent
GRAPH_PATH = BASE_DIR / "web" / "graph.json"
DB_PATH = BASE_DIR / "data" / "navigatela.db"

# Approximate meters per degree at DTLA latitude (~34°N)
LAT_M = 111_320.0
LON_M = 92_400.0  # 111320 * cos(34°)

MATCH_RADIUS_M = 50.0


def load_signal_timing(conn):
    """Load signal timing keyed by (traffic_id, period)."""
    rows = conn.execute(
        "SELECT traffic_id, period, hcm_cycle, hcm_phases FROM signal_timing"
    ).fetchall()
    timing = {}
    for tid, period, cycle, phases_json in rows:
        if not phases_json or not cycle:
            continue
        try:
            phases = json.loads(phases_json)
        except (json.JSONDecodeError, TypeError):
            continue
        # Sum green time for N/S phases (dirs containing NB or SB)
        ns_green = 0.0
        for ph in phases:
            dirs = ph.get("dirs", [])
            if any("NB" in d or "SB" in d for d in dirs):
                ns_green += ph.get("green", 0.0)
        timing[(tid, period)] = (cycle, ns_green)
    return timing


def main():
    print(f"Loading {GRAPH_PATH}...")
    with open(GRAPH_PATH) as f:
        graph = json.load(f)

    print(f"Loading {DB_PATH}...")
    conn = sqlite3.connect(str(DB_PATH))

    # Load intersections
    ints = conn.execute(
        "SELECT traffic_id, lat, lon FROM intersections"
    ).fetchall()
    if not ints:
        print("No intersections found in DB")
        return

    int_ids = [r[0] for r in ints]
    int_coords = np.array([[r[1], r[2]] for r in ints])
    # Convert to meters for KDTree
    int_xy = np.column_stack([
        int_coords[:, 0] * LAT_M,
        int_coords[:, 1] * LON_M,
    ])
    tree = KDTree(int_xy)
    print(f"Built KDTree from {len(ints)} intersections")

    # Load signal timing
    timing = load_signal_timing(conn)
    conn.close()
    print(f"Loaded {len(timing)} signal timing records")

    # Match graph nodes
    matched = 0
    for node in graph["nodes"]:
        if not node.get("light"):
            continue
        lat = node.get("lat")
        lon = node.get("lon")
        if lat is None or lon is None:
            continue

        query_xy = [lat * LAT_M, lon * LON_M]
        dist, idx = tree.query(query_xy)
        if dist > MATCH_RADIUS_M:
            continue

        tid = int_ids[idx]

        # Extract per-period timing
        periods = {}
        for period in ("AM", "PM", "MID", "OFF"):
            key = (tid, period)
            if key in timing:
                periods[period] = timing[key]

        if not periods:
            continue

        # Build 8-element signal array
        am = periods.get("AM")
        pm = periods.get("PM")
        mid = periods.get("MID")
        off = periods.get("OFF")

        # Fallbacks
        if mid is None and am is not None:
            mid = am
        if off is None and am is not None:
            # Off-peak: use AM cycle with 1.5x NS green ratio (more balanced)
            cycle, ns_green = am
            off_ns = min(ns_green * 1.5, cycle * 0.5)
            off = (cycle, off_ns)

        # Only add if we have at least AM data
        if am is None:
            continue

        if pm is None:
            pm = am

        signal = [
            am[0], am[1],      # AM cycle, ns_green
            pm[0], pm[1],      # PM cycle, ns_green
            mid[0], mid[1],    # MID cycle, ns_green
            off[0], off[1],    # OFF cycle, ns_green
        ]
        # Round to 1 decimal
        node["signal"] = [round(v, 1) for v in signal]
        matched += 1

    print(f"Matched {matched} signal nodes")

    # Write back
    print(f"Writing {GRAPH_PATH}...")
    with open(GRAPH_PATH, "w") as f:
        json.dump(graph, f, separators=(",", ":"))
    print("Done")


if __name__ == "__main__":
    main()
