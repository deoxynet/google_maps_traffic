#!/usr/bin/env python3
"""Enrich web/graph.json with parking capacity from OSM + real parking DB + satellite.

Sources (in priority order):
  1. OSM parking features (amenity=parking nodes/ways from data/osm_features.json)
  2. Street meters, ABM facilities, LAZ garages from park_smarter_meters.db
  3. Satellite-detected parking spots (data/satellite_parking.db from detect_parked_cars.py)

Snaps each parking location to the nearest graph node via KDTree and aggregates
total spaces per node. Adds "parking": N to matched nodes in graph.json.
"""

import json
import sqlite3
from pathlib import Path

import numpy as np
from scipy.spatial import KDTree

BASE_DIR = Path(__file__).resolve().parent.parent
GRAPH_PATH = BASE_DIR / "web" / "graph.json"
OSM_FEATURES_PATH = BASE_DIR / "data" / "osm_features.json"
SAT_DB_PATH = BASE_DIR / "data" / "satellite_parking.db"
DB_PATH = Path.home() / "git" / "la-parking" / "db" / "park_smarter_meters.db"

# Coordinate transform: lat/lon → graph pixel coords (from linear regression)
LON_TO_X_SCALE = 23301.6864
LON_TO_X_OFFSET = 2758399.7075
LAT_TO_Y_SCALE = -28110.8522
LAT_TO_Y_OFFSET = 957249.4208

# Graph bounding box in lat/lon
LAT_MIN, LAT_MAX = 33.96, 34.07
LON_MIN, LON_MAX = -118.40, -118.25

# Max distance in pixels to match parking to a graph node
MATCH_RADIUS_PX = 50.0  # ~200m


def latlon_to_px(lat, lon):
    x = LON_TO_X_SCALE * lon + LON_TO_X_OFFSET
    y = LAT_TO_Y_SCALE * lat + LAT_TO_Y_OFFSET
    return x, y


def in_bbox(lat, lon):
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


def load_osm_parking(osm_path):
    """Load parking features from OSM extraction (data/osm_features.json)."""
    with open(osm_path) as f:
        osm = json.load(f)

    features = osm.get("parking", [])
    locations = []
    for p in features:
        lat, lon = p["lat"], p["lon"]
        if not in_bbox(lat, lon):
            continue
        x, y = latlon_to_px(lat, lon)
        capacity = p.get("capacity", 20)
        locations.append((x, y, capacity, p.get("kind", "surface")))
    return locations


def load_db_parking(db_path):
    """Load parking from the real DB (meters + facilities + garages)."""
    locations = []
    conn = sqlite3.connect(str(db_path))

    # Street meters (1 space each)
    cur = conn.execute(
        "SELECT lat, lon FROM meters WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (LAT_MIN, LAT_MAX, LON_MIN, LON_MAX),
    )
    meter_count = 0
    for lat, lon in cur.fetchall():
        if lat and lon:
            x, y = latlon_to_px(lat, lon)
            locations.append((x, y, 1, "meter"))
            meter_count += 1

    # ABM facilities
    cur = conn.execute(
        "SELECT lat, lon, space_total FROM abm_locations "
        "WHERE kind='facility' AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (LAT_MIN, LAT_MAX, LON_MIN, LON_MAX),
    )
    fac_count = 0
    for lat, lon, spaces in cur.fetchall():
        if lat and lon and spaces and spaces > 0:
            x, y = latlon_to_px(lat, lon)
            locations.append((x, y, int(spaces), "facility"))
            fac_count += 1

    # LAZ garages
    cur = conn.execute(
        "SELECT lat, lon, total_spaces FROM laz_locations "
        "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (LAT_MIN, LAT_MAX, LON_MIN, LON_MAX),
    )
    gar_count = 0
    for lat, lon, spaces in cur.fetchall():
        if lat and lon and spaces:
            try:
                s = int(spaces)
            except (ValueError, TypeError):
                continue
            if s > 0:
                x, y = latlon_to_px(lat, lon)
                locations.append((x, y, s, "garage"))
                gar_count += 1

    conn.close()
    return locations, meter_count, fac_count, gar_count


def load_satellite_parking(db_path):
    """Load satellite parking spots from SQLite (data/satellite_parking.db)."""
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT lat, lon, times_occupied FROM parking_spots "
        "WHERE graph_node_idx IS NOT NULL"
    ).fetchall()
    conn.close()

    locations = []
    for lat, lon, count in rows:
        if not in_bbox(lat, lon):
            continue
        x, y = latlon_to_px(lat, lon)
        locations.append((x, y, count, "satellite"))
    return locations


def main():
    print(f"Loading graph from {GRAPH_PATH}...")
    with open(GRAPH_PATH) as f:
        graph = json.load(f)

    nodes = graph["nodes"]
    print(f"  {len(nodes)} nodes")

    # Build KDTree of graph nodes in pixel coords
    node_xy = np.array([[n["x"], n["y"]] for n in nodes])
    node_tree = KDTree(node_xy)

    # ── Source 1: OSM parking ──
    all_parking = []
    if OSM_FEATURES_PATH.exists():
        osm_parking = load_osm_parking(OSM_FEATURES_PATH)
        print(f"\nOSM parking in bbox: {len(osm_parking)}")
        osm_spaces = sum(s for _, _, s, _ in osm_parking)
        print(f"  Total OSM spaces: {osm_spaces}")

        # Break down by kind
        from collections import Counter
        kinds = Counter(k for _, _, _, k in osm_parking)
        for kind, count in kinds.most_common():
            spaces = sum(s for _, _, s, k in osm_parking if k == kind)
            print(f"    {kind}: {count} locations, {spaces} spaces")

        all_parking.extend(osm_parking)
    else:
        print(f"\nWarning: {OSM_FEATURES_PATH} not found, skipping OSM parking")

    # ── Source 2: Real DB ──
    if DB_PATH.exists():
        db_parking, meters, facs, gars = load_db_parking(DB_PATH)
        db_spaces = sum(s for _, _, s, _ in db_parking)
        print(f"\nDB parking in bbox: {len(db_parking)} ({db_spaces} spaces)")
        print(f"  Meters: {meters}, Facilities: {facs}, Garages: {gars}")
        all_parking.extend(db_parking)
    else:
        print(f"\nWarning: {DB_PATH} not found, skipping DB parking")

    # ── Source 3: Satellite-detected parking spots ──
    if SAT_DB_PATH.exists():
        sat_parking = load_satellite_parking(SAT_DB_PATH)
        sat_spaces = sum(s for _, _, s, _ in sat_parking)
        print(f"\nSatellite parking in bbox: {len(sat_parking)} ({sat_spaces} spaces)")
        all_parking.extend(sat_parking)
    else:
        print(f"\nWarning: {SAT_DB_PATH} not found, skipping satellite parking")

    total_spaces = sum(s for _, _, s, _ in all_parking)
    print(f"\nCombined: {len(all_parking)} locations, {total_spaces} spaces")

    # ── Match to graph nodes ──
    node_spaces = {}
    matched = 0
    unmatched = 0

    for x, y, spaces, _kind in all_parking:
        dist, idx = node_tree.query([x, y])
        if dist <= MATCH_RADIUS_PX:
            node_spaces[idx] = node_spaces.get(idx, 0) + spaces
            matched += 1
        else:
            unmatched += 1

    print(f"\n  Matched: {matched}, unmatched: {unmatched}")
    print(f"  Unique nodes with parking: {len(node_spaces)}")
    matched_total = sum(node_spaces.values())
    print(f"  Total matched spaces: {matched_total}")

    # Clear old parking data and write
    for node in nodes:
        node.pop("parking", None)

    for idx, spaces in node_spaces.items():
        nodes[idx]["parking"] = spaces

    # Stats
    caps = sorted(node_spaces.values(), reverse=True)
    if caps:
        print(f"\n  Top 10 nodes by capacity: {caps[:10]}")
        print(f"  Median capacity: {caps[len(caps) // 2]}")

    print(f"\nWriting {GRAPH_PATH}...")
    with open(GRAPH_PATH, "w") as f:
        json.dump(graph, f, separators=(",", ":"))

    print("Done")


if __name__ == "__main__":
    main()
