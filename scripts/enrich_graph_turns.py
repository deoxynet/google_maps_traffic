#!/usr/bin/env python3
"""Enrich web/graph.json nodes with turn-movement ratios from navigatela.db.

For each graph node with light=true, spatially matches to the nearest
intersection in the DB (within 50m) and extracts per-approach turn ratios.

Adds to each matched node:
  - "turns": [12 floats] — EB_L,EB_T,EB_R, WB_L,WB_T,WB_R, NB_L,NB_T,NB_R, SB_L,SB_T,SB_R
  - "vol": float — peak hourly volume (max total across approaches and periods)
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

# Direction order for the 12-float turn array
DIR_ORDER = ["EB", "WB", "NB", "SB"]
DEFAULT_RATIOS = [0.10, 0.80, 0.10]


def build_hourly_volume_profile(conn):
    """Build a 24-element hourly volume profile from PeMS continuous sensor data.

    Uses mainline (ML) freeway detector data which provides uniform 24-hour
    coverage across thousands of sensors — much better than manual intersection
    counts which heavily sample peak hours.

    Falls back to manual count data if PeMS is unavailable.

    Returns: list of 24 floats normalized to [0, 1]
    """
    pems_path = Path(__file__).parent.parent / "data" / "pems.db"
    if pems_path.exists():
        pems_conn = sqlite3.connect(str(pems_path))
        rows = pems_conn.execute("""
            SELECT hour, AVG(flow) as avg_flow
            FROM hourly_flow
            WHERE type = 'ML'
            GROUP BY hour
            ORDER BY hour
        """).fetchall()
        pems_conn.close()

        if len(rows) >= 24:
            profile = [0.0] * 24
            for hour, avg_flow in rows:
                profile[int(hour)] = avg_flow
            peak = max(profile) if max(profile) > 0 else 1.0
            profile = [round(v / peak, 4) for v in profile]
            print(f"  Hourly profile: PeMS mainline sensors ({len(rows)} hours)")
            return profile

    # Fallback: manual counts (biased toward peak hours)
    print("  Hourly profile: manual counts (PeMS unavailable)")
    rows = conn.execute("""
        SELECT m.hour_start as hour,
               SUM(m.total) * 1.0 / count(DISTINCT m.traffic_id) as vol_per_int
        FROM manual_counts m
        WHERE m.approach IN ('NB','SB','EB','WB') AND m.total > 0
        GROUP BY m.hour_start
        ORDER BY m.hour_start
    """).fetchall()

    profile = [0.0] * 24
    for hour, vol_per_int in rows:
        profile[int(hour)] = vol_per_int

    # Interpolate gaps
    for h in range(24):
        if profile[h] == 0.0:
            prev_h, next_h = h, h
            for _ in range(12):
                prev_h = (prev_h - 1) % 24
                if profile[prev_h] > 0:
                    break
            for _ in range(12):
                next_h = (next_h + 1) % 24
                if profile[next_h] > 0:
                    break
            if profile[prev_h] > 0 and profile[next_h] > 0:
                profile[h] = (profile[prev_h] + profile[next_h]) / 2
            elif profile[prev_h] > 0:
                profile[h] = profile[prev_h]
            elif profile[next_h] > 0:
                profile[h] = profile[next_h]

    peak = max(profile) if max(profile) > 0 else 1.0
    profile = [round(v / peak, 4) for v in profile]
    return profile


def load_turn_data(conn):
    """Load turn-movement data from signal_timing.

    For each traffic_id, picks the period with highest total volume (typically AM or PM)
    and extracts per-approach left/thru/right ratios.

    Returns:
        turns: {traffic_id: {dir: (left_ratio, thru_ratio, right_ratio)}}
        volumes: {traffic_id: peak_hourly_volume}
    """
    rows = conn.execute(
        "SELECT traffic_id, period, approaches FROM signal_timing "
        "WHERE approaches IS NOT NULL AND approaches != '[]'"
    ).fetchall()

    # Group by traffic_id, pick best period (highest total volume)
    by_tid = {}  # tid -> [(period, approaches_json)]
    for tid, period, approaches_json in rows:
        if tid not in by_tid:
            by_tid[tid] = []
        by_tid[tid].append((period, approaches_json))

    turns = {}   # tid -> {dir: (l_ratio, t_ratio, r_ratio)}
    volumes = {} # tid -> peak_hourly_volume

    for tid, period_rows in by_tid.items():
        # Find the period with highest total volume
        best_total = 0
        best_approaches = None
        peak_vol = 0

        for period, approaches_json in period_rows:
            try:
                approaches = json.loads(approaches_json)
            except (json.JSONDecodeError, TypeError):
                continue

            period_total = sum(a.get("total", 0) for a in approaches)
            peak_vol = max(peak_vol, period_total)

            if period_total > best_total:
                best_total = period_total
                best_approaches = approaches

        if not best_approaches:
            continue

        # Extract per-approach ratios
        tid_turns = {}
        for a in best_approaches:
            d = a.get("dir", "")
            if d not in DIR_ORDER:
                continue
            total = a.get("total", 0)
            if total <= 0:
                tid_turns[d] = tuple(DEFAULT_RATIOS)
                continue
            left = a.get("left", 0)
            thru = a.get("thru", 0)
            right = a.get("right", 0)
            s = left + thru + right
            if s <= 0:
                tid_turns[d] = tuple(DEFAULT_RATIOS)
                continue
            tid_turns[d] = (left / s, thru / s, right / s)

        if tid_turns:
            turns[tid] = tid_turns
            volumes[tid] = peak_vol

    return turns, volumes


def main():
    print(f"Loading {GRAPH_PATH}...")
    with open(GRAPH_PATH) as f:
        graph = json.load(f)

    print(f"Loading {DB_PATH}...")
    conn = sqlite3.connect(str(DB_PATH))

    # Load intersections for spatial matching
    ints = conn.execute(
        "SELECT traffic_id, lat, lon FROM intersections"
    ).fetchall()
    if not ints:
        print("No intersections found in DB")
        return

    int_ids = [r[0] for r in ints]
    int_coords = np.array([[r[1], r[2]] for r in ints])
    int_xy = np.column_stack([
        int_coords[:, 0] * LAT_M,
        int_coords[:, 1] * LON_M,
    ])
    tree = KDTree(int_xy)
    print(f"Built KDTree from {len(ints)} intersections")

    # Load turn data
    turns, volumes = load_turn_data(conn)

    # Build hourly volume profile from count data
    hourly_profile = build_hourly_volume_profile(conn)
    print(f"Hourly volume profile (normalized):")
    for h in range(24):
        bar = "█" * int(hourly_profile[h] * 40)
        print(f"  {h:2d}:00  {hourly_profile[h]:.3f}  {bar}")

    conn.close()
    print(f"Loaded turn data for {len(turns)} intersections")

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
        if tid not in turns:
            continue

        tid_turns = turns[tid]

        # Build 12-float array: [EB_L,EB_T,EB_R, WB_L,WB_T,WB_R, NB_L,NB_T,NB_R, SB_L,SB_T,SB_R]
        turn_array = []
        for d in DIR_ORDER:
            if d in tid_turns:
                l, t, r = tid_turns[d]
                turn_array.extend([round(l, 4), round(t, 4), round(r, 4)])
            else:
                turn_array.extend([0.0, 0.0, 0.0])

        node["turns"] = turn_array
        node["vol"] = volumes.get(tid, 0)
        matched += 1

    print(f"Matched {matched} nodes with turn data")

    # Add hourly volume profile to graph
    graph["hourly_volume"] = hourly_profile
    total_counted_vol = sum(volumes.values())
    graph["total_counted_volume"] = round(total_counted_vol)
    print(f"Total counted peak volume across matched nodes: {total_counted_vol:.0f} veh/hr")

    # ── Compute data-driven peak car count via Little's Law ──
    # For each edge, estimate flow from endpoint volumes and compute expected
    # instantaneous occupancy = flow * travel_time.
    # Then extrapolate from counted nodes to full network.
    import math
    METERS_PER_PIXEL = 3.9476
    node_map = {n["id"]: n for n in graph["nodes"]}
    edges = graph["edges"]

    # Build node degree (number of edges per node)
    node_degree = {}
    for e in edges:
        node_degree[e["from"]] = node_degree.get(e["from"], 0) + 1
        node_degree[e["to"]] = node_degree.get(e["to"], 0) + 1

    counted_cars = 0.0  # expected cars on edges touching counted nodes
    counted_edge_len = 0.0
    total_edge_len = 0.0

    for e in edges:
        pts = e["pts"]
        elen_px = 0.0
        for i in range(1, len(pts)):
            dx = pts[i][0] - pts[i - 1][0]
            dy = pts[i][1] - pts[i - 1][1]
            elen_px += math.sqrt(dx * dx + dy * dy)
        edge_len_m = elen_px * METERS_PER_PIXEL
        total_edge_len += edge_len_m

        speed_ms = e["speed"] * (1000.0 / 3600.0)
        travel_time_hr = (edge_len_m / max(speed_ms, 0.1)) / 3600.0

        v_from = node_map.get(e["from"], {}).get("vol", 0) or 0
        v_to = node_map.get(e["to"], {}).get("vol", 0) or 0

        if v_from > 0 or v_to > 0:
            deg_from = max(node_degree.get(e["from"], 2), 1)
            deg_to = max(node_degree.get(e["to"], 2), 1)
            flow = (v_from / deg_from + v_to / deg_to) / 2.0
            counted_cars += flow * travel_time_hr
            counted_edge_len += edge_len_m

    # Extrapolate: uncounted roads have similar density per km
    if counted_edge_len > 0:
        density_per_m = counted_cars / counted_edge_len
        peak_cars = round(density_per_m * total_edge_len)
    else:
        peak_cars = 10000  # fallback

    graph["peak_cars"] = peak_cars
    print(f"Little's Law peak cars: {counted_cars:.0f} (counted edges)")
    print(f"  Counted road: {counted_edge_len / 1000:.0f} km / {total_edge_len / 1000:.0f} km total")
    print(f"  Extrapolated peak_cars: {peak_cars}")

    # Write back
    print(f"Writing {GRAPH_PATH}...")
    with open(GRAPH_PATH, "w") as f:
        json.dump(graph, f, separators=(",", ":"))

    size_mb = GRAPH_PATH.stat().st_size / 1_048_576
    print(f"Done ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
