#!/usr/bin/env python3
"""Enrich web/graph.json with OSM traffic control types and lane counts.

Reads data/osm_features.json (from extract_osm_features) and reclassifies
graph nodes: real traffic signals get light=true, stop signs get stop=true,
and edges get lane counts for capacity modeling.

Run BEFORE enrich_graph_signals.py and interpolate_signals.py.
"""

import json
import math
from pathlib import Path

import numpy as np
from scipy.spatial import KDTree

BASE_DIR = Path(__file__).resolve().parent.parent
GRAPH_PATH = BASE_DIR / "web" / "graph.json"
OSM_FEATURES_PATH = BASE_DIR / "data" / "osm_features.json"

# Approximate meters per degree at DTLA latitude (~34°N)
LAT_M = 111_320.0
LON_M = 92_400.0  # 111320 * cos(34°)

MATCH_RADIUS_M = 30.0


def bearing_between(lat0, lon0, lat1, lon1):
    """Compute bearing in radians from point 0 to point 1."""
    dy = (lat1 - lat0) * LAT_M
    dx = (lon1 - lon0) * LON_M
    return math.atan2(dx, dy)


def bearing_diff(b1, b2):
    """Absolute angular difference between two bearings."""
    d = abs(b1 - b2) % (2 * math.pi)
    if d > math.pi:
        d = 2 * math.pi - d
    return d


def main():
    print(f"Loading {OSM_FEATURES_PATH}...")
    with open(OSM_FEATURES_PATH) as f:
        osm = json.load(f)

    print(f"Loading {GRAPH_PATH}...")
    with open(GRAPH_PATH) as f:
        graph = json.load(f)

    nodes = graph["nodes"]
    edges = graph["edges"]
    print(f"  {len(nodes)} nodes, {len(edges)} edges")

    old_light_count = sum(1 for n in nodes if n.get("light"))
    print(f"  Current light=true: {old_light_count}")

    # ── Step 0: Clear stale fields from previous runs ────────────
    for node in nodes:
        node.pop("stop", None)

    # ── Step 1: Build KDTrees for OSM features ─────────────────────

    # Signal nodes
    raw_signals = osm.get("signal_nodes", [])
    if raw_signals:
        signal_coords = np.array(raw_signals)
        signal_xy = np.column_stack([
            signal_coords[:, 0] * LAT_M,
            signal_coords[:, 1] * LON_M,
        ])
        signal_tree = KDTree(signal_xy)
        print(f"  Signal KDTree: {len(signal_coords)} nodes")
    else:
        signal_tree = None
        print("  Warning: No signal nodes in OSM data")

    # Stop nodes
    raw_stops = osm.get("stop_nodes", [])
    if raw_stops:
        stop_coords = np.array(raw_stops)
        stop_xy = np.column_stack([
            stop_coords[:, 0] * LAT_M,
            stop_coords[:, 1] * LON_M,
        ])
        stop_tree = KDTree(stop_xy)
        print(f"  Stop KDTree: {len(stop_coords)} nodes")
    else:
        stop_tree = None
        print("  Warning: No stop nodes in OSM data")

    # Way segments (for lane matching)
    segments = osm.get("way_segments", [])
    if segments:
        seg_mids = []
        seg_bearings = []
        seg_lanes = []
        for seg in segments:
            lat0, lon0 = seg["start"]
            lat1, lon1 = seg["end"]
            mid_lat = (lat0 + lat1) / 2
            mid_lon = (lon0 + lon1) / 2
            seg_mids.append([mid_lat * LAT_M, mid_lon * LON_M])
            seg_bearings.append(bearing_between(lat0, lon0, lat1, lon1))
            seg_lanes.append(seg["lanes"])
        seg_mid_arr = np.array(seg_mids)
        seg_tree = KDTree(seg_mid_arr)
        print(f"  Segment KDTree: {len(segments)} segments")
    else:
        seg_tree = None
        print("  Warning: No way segments in OSM data")

    # ── Step 2: Reclassify light field ─────────────────────────────

    # Identify nodes that already have NavigateLA signal data
    # (have both 'signal' and 'vol' fields from enrich_graph_signals.py)
    navigatela_nodes = set()
    for i, node in enumerate(nodes):
        if node.get("signal") and node.get("vol") is not None:
            navigatela_nodes.add(i)

    new_light_count = 0
    cleared_light_count = 0

    for i, node in enumerate(nodes):
        lat = node.get("lat")
        lon = node.get("lon")
        if lat is None or lon is None:
            continue

        query_xy = [lat * LAT_M, lon * LON_M]

        # Check OSM signal match
        osm_signal = False
        if signal_tree is not None:
            dist, _ = signal_tree.query(query_xy)
            if dist <= MATCH_RADIUS_M:
                osm_signal = True

        # Keep light=true if NavigateLA matched OR OSM signal matched
        if osm_signal or i in navigatela_nodes:
            node["light"] = True
            new_light_count += 1
        else:
            if node.get("light"):
                cleared_light_count += 1
            node["light"] = False

    print(f"\n  Reclassified light:")
    print(f"    light=true: {old_light_count} → {new_light_count}")
    print(f"    Cleared: {cleared_light_count}")

    # ── Step 3: Add stop field ─────────────────────────────────────

    stop_count = 0
    for node in nodes:
        if node.get("light"):
            continue  # signals don't get stop

        lat = node.get("lat")
        lon = node.get("lon")
        if lat is None or lon is None:
            continue

        if stop_tree is not None:
            query_xy = [lat * LAT_M, lon * LON_M]
            dist, _ = stop_tree.query(query_xy)
            if dist <= MATCH_RADIUS_M:
                node["stop"] = True
                stop_count += 1

    print(f"    stop=true: {stop_count}")

    # ── Step 4: Clean up stale signal data ─────────────────────────

    cleaned_signal = 0
    for node in nodes:
        if not node.get("light"):
            if "signal" in node and node.get("vol") is None:
                # Remove interpolated signal data from nodes that lost light status
                # (includes stop-sign nodes that shouldn't have signal timing)
                del node["signal"]
                cleaned_signal += 1

    print(f"    Cleaned stale signal arrays: {cleaned_signal}")

    # ── Step 5: Add lanes to edges ─────────────────────────────────

    lanes_matched = 0
    lanes_defaulted = 0

    for edge in edges:
        pts = edge.get("pts", [])
        if len(pts) < 2:
            continue

        matched_lanes = None

        if seg_tree is not None and len(pts) >= 2:
            # Compute edge midpoint and bearing
            # Use the graph's pixel-space pts — but we need lat/lon for matching.
            # Since pts are in pixel space, we need the edge's from/to nodes for lat/lon.
            from_node = nodes[edge["from"]]
            to_node = nodes[edge["to"]]
            from_lat = from_node.get("lat")
            from_lon = from_node.get("lon")
            to_lat = to_node.get("lat")
            to_lon = to_node.get("lon")

            if from_lat is not None and to_lat is not None:
                mid_lat = (from_lat + to_lat) / 2
                mid_lon = (from_lon + to_lon) / 2
                edge_bearing = bearing_between(from_lat, from_lon, to_lat, to_lon)

                query_xy = [mid_lat * LAT_M, mid_lon * LON_M]
                # Query multiple candidates
                dists, idxs = seg_tree.query(query_xy, k=min(5, len(seg_mids)))
                if not hasattr(dists, '__len__'):
                    dists = [dists]
                    idxs = [idxs]

                for dist, idx in zip(dists, idxs):
                    if dist > MATCH_RADIUS_M:
                        break
                    # Bearing check: edge and segment should be roughly parallel
                    seg_b = seg_bearings[idx]
                    diff = bearing_diff(edge_bearing, seg_b)
                    # Accept if within 45° or within 45° of opposite direction
                    if diff < math.pi / 4 or diff > 3 * math.pi / 4:
                        matched_lanes = seg_lanes[idx]
                        break

        if matched_lanes is not None:
            edge["lanes"] = matched_lanes
            lanes_matched += 1
        else:
            # Speed-based defaults
            speed = edge.get("speed", 25)
            if speed >= 80:
                edge["lanes"] = 3
            elif speed >= 55:
                edge["lanes"] = 2
            else:
                edge["lanes"] = 1
            lanes_defaulted += 1

    print(f"\n  Edge lanes:")
    print(f"    OSM-matched: {lanes_matched}")
    print(f"    Speed-default: {lanes_defaulted}")

    # Lane distribution
    from collections import Counter
    lane_dist = Counter(e.get("lanes", 1) for e in edges)
    print(f"    Distribution: {dict(sorted(lane_dist.items()))}")

    # ── Step 6: Write back ─────────────────────────────────────────

    print(f"\nWriting {GRAPH_PATH}...")
    with open(GRAPH_PATH, "w") as f:
        json.dump(graph, f, separators=(",", ":"))

    # Final summary
    final_light = sum(1 for n in nodes if n.get("light"))
    final_stop = sum(1 for n in nodes if n.get("stop"))
    final_neither = sum(1 for n in nodes if not n.get("light") and not n.get("stop"))
    print(f"\nFinal node classification:")
    print(f"  light=true:  {final_light}")
    print(f"  stop=true:   {final_stop}")
    print(f"  uncontrolled: {final_neither}")
    print("Done")


if __name__ == "__main__":
    main()
