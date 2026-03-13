#!/usr/bin/env python3
"""Interpolate signal timing for unmatched light=true nodes in web/graph.json.

Runs AFTER enrich_graph_signals.py. Fills every remaining light=true node
(no existing signal array) with an 8-element signal array using a 3-tier
fallback: BFS neighbor interpolation → speed-class heuristic → regional default.

Never overwrites existing signal data.
"""

import json
import math
from collections import deque
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
GRAPH_PATH = BASE_DIR / "web" / "graph.json"

# Standard cycle lengths to snap to
STANDARD_CYCLES = [60, 75, 90, 120, 150, 180]

# BFS parameters
BFS_MAX_HOPS = 15
BFS_MAX_NEIGHBORS = 5


def snap_cycle(value):
    """Snap a cycle length to the nearest standard value."""
    return min(STANDARD_CYCLES, key=lambda c: abs(c - value))


def edge_bearing(edge):
    """Compute arrival bearing at the 'to' end of an edge, matching Rust convention.

    Rust: atan2(bx - ax, by - ay) using the last two pts, y-down coordinate system.
    """
    pts = edge["pts"]
    if len(pts) < 2:
        return 0.0
    ax, ay = pts[-2]
    bx, by = pts[-1]
    return math.atan2(bx - ax, by - ay)


def is_ns(bearing):
    """Classify bearing as NS (True) or EW (False).

    Matches sim.rs: abs(bearing) < pi/4 or abs(bearing) > 3*pi/4
    """
    ab = abs(bearing)
    return ab < math.pi / 4 or ab > 3 * math.pi / 4


def compute_green_ratio_from_edges(node_id, edges, nodes):
    """Estimate NS green ratio from NS/EW speed balance at a node.

    Returns a ratio in [0.30, 0.60] or None if no edges found.
    """
    ns_speeds = []
    ew_speeds = []

    node = nodes[node_id]
    for ei in node.get("edges", []):
        e = edges[ei]
        # Compute bearing toward this node
        if e["to"] == node_id:
            b = edge_bearing(e)
        elif e["from"] == node_id:
            # Reverse direction: flip bearing
            pts = e["pts"]
            if len(pts) < 2:
                continue
            ax, ay = pts[1]
            bx, by = pts[0]
            b = math.atan2(bx - ax, by - ay)
        else:
            continue

        speed = e.get("speed", 25.0)
        if is_ns(b):
            ns_speeds.append(speed)
        else:
            ew_speeds.append(speed)

    if not ns_speeds and not ew_speeds:
        return None

    ns_avg = sum(ns_speeds) / len(ns_speeds) if ns_speeds else 0.0
    ew_avg = sum(ew_speeds) / len(ew_speeds) if ew_speeds else 0.0
    total = ns_avg + ew_avg
    if total == 0:
        return 0.42  # dataset median fallback

    # Map ns_fraction linearly to [0.30, 0.60]
    ns_frac = ns_avg / total
    return 0.30 + ns_frac * 0.30


def build_adjacency(nodes, edges):
    """Build adjacency list: node_id -> list of (neighbor_node_id, hop_distance=1)."""
    adj = [[] for _ in range(len(nodes))]
    for e in edges:
        f, t = e["from"], e["to"]
        adj[f].append(t)
        if not e.get("oneway", False):
            adj[t].append(f)
    return adj


def bfs_find_signal_neighbors(start, adj, nodes, signal_set):
    """BFS from start node to find up to BFS_MAX_NEIGHBORS signal nodes within BFS_MAX_HOPS.

    Stops traversal through other signal nodes (they form corridor boundaries).
    Returns list of (node_id, hop_distance).
    """
    visited = {start}
    queue = deque([(start, 0)])
    found = []

    while queue and len(found) < BFS_MAX_NEIGHBORS:
        node, hops = queue.popleft()
        if hops >= BFS_MAX_HOPS:
            continue

        for neighbor in adj[node]:
            if neighbor in visited:
                continue
            visited.add(neighbor)
            nhops = hops + 1

            if neighbor in signal_set:
                found.append((neighbor, nhops))
                # Don't traverse through signal nodes
                continue

            queue.append((neighbor, nhops))

    return found


def idw_average(values_distances):
    """Inverse-distance-weighted average. values_distances: [(value, distance)]."""
    if not values_distances:
        return None
    if len(values_distances) == 1:
        return values_distances[0][0]

    w_sum = 0.0
    v_sum = 0.0
    for val, dist in values_distances:
        w = 1.0 / max(dist, 0.5)  # avoid division by zero
        w_sum += w
        v_sum += w * val
    return v_sum / w_sum if w_sum > 0 else None


def main():
    print(f"Loading {GRAPH_PATH}...")
    with open(GRAPH_PATH) as f:
        graph = json.load(f)

    nodes = graph["nodes"]
    edges = graph["edges"]
    n_nodes = len(nodes)
    n_edges = len(edges)
    print(f"  {n_nodes} nodes, {n_edges} edges")

    # Identify light nodes with and without signal data
    light_with_signal = set()
    light_without_signal = []
    for i, node in enumerate(nodes):
        if not node.get("light"):
            continue
        if node.get("signal"):
            light_with_signal.add(i)
        else:
            light_without_signal.append(i)

    print(f"  Light nodes with signal: {len(light_with_signal)}")
    print(f"  Light nodes without signal: {len(light_without_signal)}")

    if not light_without_signal:
        print("Nothing to do.")
        return

    # Build adjacency
    print("Building adjacency list...")
    adj = build_adjacency(nodes, edges)

    # Pre-extract signal data for BFS lookups: node_id -> (cycle, green_ratio)
    signal_data = {}
    for nid in light_with_signal:
        sig = nodes[nid]["signal"]
        cycle = sig[0]  # AM cycle
        if cycle > 0:
            green_ratio = sig[1] / cycle
            signal_data[nid] = (cycle, green_ratio)

    # Process each unmatched node
    tier1_count = 0
    tier2_count = 0
    tier3_count = 0

    interp_cycles = []
    interp_ratios = []

    print("Interpolating signal timing...")
    for nid in light_without_signal:
        cycle = None
        green_ratio = None
        tier = 0

        # Tier 1: BFS neighbor interpolation
        neighbors = bfs_find_signal_neighbors(nid, adj, nodes, light_with_signal)
        neighbor_data = []
        for nbr_id, dist in neighbors:
            if nbr_id in signal_data:
                neighbor_data.append((signal_data[nbr_id], dist))

        if neighbor_data:
            # IDW average of cycle lengths
            cycle_idw = idw_average([(d[0], dist) for d, dist in neighbor_data])
            cycle = snap_cycle(cycle_idw)

            # IDW average of green ratios
            ratio_idw = idw_average([(d[1], dist) for d, dist in neighbor_data])

            # Edge-based green ratio estimate
            edge_ratio = compute_green_ratio_from_edges(nid, edges, nodes)

            if edge_ratio is not None:
                # 70/30 blend: BFS neighbors + local edge geometry
                green_ratio = 0.7 * ratio_idw + 0.3 * edge_ratio
            else:
                green_ratio = ratio_idw

            tier = 1
            tier1_count += 1

        # Tier 2: Speed-class heuristic
        if cycle is None:
            node = nodes[nid]
            max_speed = 0.0
            for ei in node.get("edges", []):
                s = edges[ei].get("speed", 0.0)
                if s > max_speed:
                    max_speed = s

            if max_speed >= 50:
                cycle = 90
            elif max_speed >= 35:
                cycle = 75
            else:
                cycle = 60

            edge_ratio = compute_green_ratio_from_edges(nid, edges, nodes)
            green_ratio = edge_ratio if edge_ratio is not None else 0.42

            tier = 2
            tier2_count += 1

        # Tier 3: Regional default
        if cycle is None:
            cycle = 60
            green_ratio = 0.42
            tier = 3
            tier3_count += 1

        # Clamp green ratio to [0.30, 0.60]
        green_ratio = max(0.30, min(0.60, green_ratio))

        # Compute ns_green from ratio
        ns_green = round(cycle * green_ratio, 1)

        # OFF period: blend green ratio toward 0.50 (more balanced off-peak)
        off_ratio = 0.6 * green_ratio + 0.4 * 0.50
        off_ns_green = round(cycle * off_ratio, 1)

        # All active periods share the same cycle and green time
        # OFF gets same cycle, blended green ratio
        signal = [
            cycle, ns_green,       # AM
            cycle, ns_green,       # PM
            cycle, ns_green,       # MID
            cycle, off_ns_green,   # OFF
        ]
        nodes[nid]["signal"] = signal

        interp_cycles.append(cycle)
        interp_ratios.append(green_ratio)

    # Summary statistics
    total_filled = tier1_count + tier2_count + tier3_count
    print(f"\nInterpolated {total_filled} signal nodes:")
    print(f"  Tier 1 (BFS neighbor): {tier1_count}")
    print(f"  Tier 2 (speed-class):  {tier2_count}")
    print(f"  Tier 3 (default):      {tier3_count}")

    # Verify coverage
    total_light = len(light_with_signal) + total_filled
    all_light = sum(1 for n in nodes if n.get("light"))
    all_with_signal = sum(1 for n in nodes if n.get("light") and n.get("signal"))
    print(f"\nCoverage: {all_with_signal}/{all_light} light nodes now have signal data")

    # Distribution stats
    if interp_cycles:
        interp_cycles.sort()
        interp_ratios.sort()
        mid = len(interp_cycles) // 2
        print(f"\nInterpolated distribution:")
        print(f"  Cycle median:       {interp_cycles[mid]}s")
        print(f"  Cycle mean:         {sum(interp_cycles)/len(interp_cycles):.1f}s")
        print(f"  Green ratio median: {interp_ratios[mid]:.3f}")
        print(f"  Green ratio mean:   {sum(interp_ratios)/len(interp_ratios):.3f}")

        from collections import Counter
        cycle_dist = Counter(interp_cycles)
        print(f"  Cycle breakdown:")
        for c in STANDARD_CYCLES:
            if cycle_dist[c]:
                print(f"    {c}s: {cycle_dist[c]}")

    # Write back
    print(f"\nWriting {GRAPH_PATH}...")
    with open(GRAPH_PATH, "w") as f:
        json.dump(graph, f, separators=(",", ":"))
    print("Done")


if __name__ == "__main__":
    main()
