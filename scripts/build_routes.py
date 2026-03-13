#!/usr/bin/env python3
"""
Build flow-balanced routes from graph.json + sensor data.

Uses LP optimization to find edge flows that:
1. Match congestion-derived targets (BPR volume-delay function)
2. Satisfy flow conservation at all interior nodes
3. Respect PeMS ramp observations where available
4. Honor NavigateLA turning movement splits where available

Then decomposes the balanced flows into source-to-sink routes.

Usage:
    python scripts/build_routes.py
    python scripts/build_routes.py --graph web/graph.json --all-hours
    python scripts/build_routes.py --hour 8 --max-routes 5000
"""

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import coo_matrix
from scipy.spatial import KDTree

try:
    import networkx as nx
except ImportError:
    sys.exit("pip install networkx")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_GRAPH = BASE_DIR / "web" / "graph.json"
NAVIGATELA_DB = BASE_DIR / "data" / "navigatela.db"
PEMS_DB = BASE_DIR / "data" / "pems.db"

# Congestion level → volume/capacity ratio (from BPR curve inversion)
VC_MAP = {0: 0.25, 1: 0.65, 2: 0.85, 3: 1.05}

# Typical urban traffic diurnal pattern (normalized, peak = 1.0)
# Based on general FHWA urban arterial profile
DIURNAL = [0.15, 0.10, 0.08, 0.07, 0.08, 0.12,   # 0-5
           0.30, 0.65, 0.85, 0.75, 0.65, 0.70,   # 6-11
           0.75, 0.75, 0.78, 0.85, 0.92, 1.00,   # 12-17
           0.80, 0.55, 0.35, 0.25, 0.20, 0.18]   # 18-23


# ── 1. Load graph ─────────────────────────────────────────────────────

def load_graph(path):
    """Load graph.json into a networkx DiGraph with proper directed edges."""
    with open(path) as f:
        data = json.load(f)

    G = nx.DiGraph()

    for n in data["nodes"]:
        G.add_node(n["id"], x=n["x"], y=n["y"],
                   lat=n.get("lat", 0), lon=n.get("lon", 0),
                   light=n["light"], edge_ids=n["edges"])

    for e in data["edges"]:
        eid = e["id"]
        pts = e["pts"]
        length = sum(
            math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
            for i in range(1, len(pts))
        )

        attrs = dict(eid=eid, pts=pts, speed=e["speed"], oneway=e["oneway"],
                     congestion=e["congestion"], length=max(length, 1.0))

        G.add_edge(e["from"], e["to"], dir=1, **attrs)
        if not e["oneway"]:
            G.add_edge(e["to"], e["from"], dir=-1, **attrs)

    return G, data


# ── 2. Match sensors to graph nodes ──────────────────────────────────

def build_kdtree(G):
    """Build KDTree from graph node coordinates."""
    ids = sorted(G.nodes())
    coords = np.array([[G.nodes[n]["lat"], G.nodes[n]["lon"]] for n in ids])
    return KDTree(coords), ids


def match_navigatela(G, tree, node_ids):
    """Match NavigateLA turning movement counts to graph nodes.

    Returns: {node_id: {hour: {approach: {left, through, right, total}}}}
    """
    if not NAVIGATELA_DB.exists():
        print("  NavigateLA DB not found")
        return {}

    conn = sqlite3.connect(NAVIGATELA_DB)
    ints = conn.execute(
        "SELECT traffic_id, lat, lon FROM intersections WHERE lat IS NOT NULL"
    ).fetchall()

    if not ints:
        conn.close()
        return {}

    matched = {}
    for tid, lat, lon in ints:
        d, i = tree.query([lat, lon])
        if d * 111_000 < 50:
            matched[tid] = node_ids[i]

    if not matched:
        conn.close()
        print("  NavigateLA: 0 matched")
        return {}

    rows = conn.execute("""
        SELECT traffic_id, hour_start, approach,
               left_turn, through, right_turn, total
        FROM manual_counts WHERE traffic_id IN ({})
    """.format(",".join(str(t) for t in matched))).fetchall()
    conn.close()

    result = defaultdict(lambda: defaultdict(dict))
    for tid, hour, appr, lt, th, rt, tot in rows:
        nid = matched[tid]
        result[nid][hour][appr] = dict(
            left=lt or 0, through=th or 0, right=rt or 0, total=tot or 0)

    n_with_data = sum(1 for v in result.values() if v)
    print(f"  NavigateLA manual: {len(matched)} intersections matched, "
          f"{n_with_data} with turning data")
    return dict(result)


def match_navigatela_auto(G, tree, node_ids):
    """Match NavigateLA automatic (tube) counts to graph nodes.

    Returns: {node_id: {hour: total_volume}}
    Tube counters measure bidirectional volume on a road segment.
    """
    if not NAVIGATELA_DB.exists():
        return {}

    conn = sqlite3.connect(NAVIGATELA_DB)

    # Get intersections that have auto counts
    rows = conn.execute("""
        SELECT DISTINCT ac.traffic_id, i.lat, i.lon
        FROM automatic_counts ac
        JOIN intersections i ON i.traffic_id = ac.traffic_id
        WHERE i.lat IS NOT NULL
    """).fetchall()

    if not rows:
        conn.close()
        return {}

    matched = {}
    for tid, lat, lon in rows:
        d, i = tree.query([lat, lon])
        if d * 111_000 < 50:
            matched[tid] = node_ids[i]

    if not matched:
        conn.close()
        print("  NavigateLA auto: 0 matched")
        return {}

    # Load hourly volumes, average across dates
    counts = conn.execute("""
        SELECT traffic_id, hour, AVG(total) as avg_total
        FROM automatic_counts
        WHERE traffic_id IN ({})
        GROUP BY traffic_id, hour
    """.format(",".join(str(t) for t in matched))).fetchall()
    conn.close()

    result = defaultdict(dict)
    for tid, hour, avg_total in counts:
        if avg_total and avg_total > 0:
            nid = matched[tid]
            # If multiple stations map to same node, sum their volumes
            result[nid][hour] = result[nid].get(hour, 0) + avg_total

    print(f"  NavigateLA auto: {len(matched)} tube counters matched, "
          f"{len(result)} nodes with data")
    return dict(result)


def match_pems(G, tree, node_ids):
    """Match PeMS ramp detectors to graph nodes.

    Returns: (sources, sinks) where each is {node_id: {hour: flow}}
    """
    if not PEMS_DB.exists():
        print("  PeMS DB not found")
        return {}, {}

    conn = sqlite3.connect(PEMS_DB)

    lats = [G.nodes[n]["lat"] for n in G.nodes()]
    lons = [G.nodes[n]["lon"] for n in G.nodes()]

    stations = conn.execute("""
        SELECT id, lat, lon, type FROM stations
        WHERE type IN ('OR','FR')
          AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
    """, (min(lats) - 0.01, max(lats) + 0.01,
          min(lons) - 0.01, max(lons) + 0.01)).fetchall()

    if not stations:
        conn.close()
        print("  PeMS: 0 stations in bounds")
        return {}, {}

    sources = defaultdict(lambda: defaultdict(float))
    sinks = defaultdict(lambda: defaultdict(float))
    n_matched = 0

    for sid, lat, lon, stype in stations:
        d, i = tree.query([lat, lon])
        if d * 111_000 > 100:
            continue
        nid = node_ids[i]
        n_matched += 1

        flows = conn.execute("""
            SELECT hour, AVG(flow) FROM hourly_flow
            WHERE station_id = ? AND flow > 0 GROUP BY hour
        """, (sid,)).fetchall()

        tgt = sources if stype == "OR" else sinks
        for h, f in flows:
            if f:
                tgt[nid][h] += f

    conn.close()
    # Net OR/FR at the same node to avoid contradictory constraints.
    # If a node has both on-ramp and off-ramp, compute net flow.
    shared = set(sources) & set(sinks)
    for nid in shared:
        src_hours = set(sources[nid].keys())
        sink_hours = set(sinks[nid].keys())
        for h in src_hours | sink_hours:
            src_flow = sources[nid].get(h, 0)
            sink_flow = sinks[nid].get(h, 0)
            net = src_flow - sink_flow
            if net > 0:
                sources[nid][h] = net
                sinks[nid].pop(h, None)
            elif net < 0:
                sinks[nid][h] = -net
                sources[nid].pop(h, None)
            else:
                sources[nid].pop(h, None)
                sinks[nid].pop(h, None)

    sources = {k: dict(v) for k, v in sources.items() if v}
    sinks = {k: dict(v) for k, v in sinks.items() if v}
    print(f"  PeMS: {n_matched} ramps matched "
          f"({len(sources)} source nodes, {len(sinks)} sink nodes, "
          f"{len(shared)} shared/netted)")
    return sources, sinks


# ── 3. Node classification ───────────────────────────────────────────

def classify_nodes(G, pems_sources, pems_sinks):
    """Classify nodes into interior (conservation-enforced) and free.

    Free nodes: boundary (degree 1) + PeMS ramp nodes.
    Interior: everything else — strict flow conservation.
    """
    boundary = set()
    for n in G.nodes():
        neighbors = set(G.predecessors(n)) | set(G.successors(n))
        if len(neighbors) <= 1:
            boundary.add(n)

    free = boundary | set(pems_sources) | set(pems_sinks)
    interior = set(G.nodes()) - free

    print(f"  {len(interior)} interior, {len(boundary)} boundary, "
          f"{len(free) - len(boundary)} ramp-only")
    return interior, boundary, free


# ── 4. BPR target flows ──────────────────────────────────────────────

def road_capacity(speed_kph):
    """Estimate hourly single-direction capacity from speed (road class proxy).

    Roughly: local ≈ 600, collector ≈ 1200, arterial ≈ 2400, freeway ≈ 5000.
    """
    return max(200, (speed_kph / 40.0) ** 1.5 * 800)


def compute_targets(G, hour):
    """Compute BPR-derived flow targets for every directed edge.

    For congestion-observed edges: the congestion level sets the V/C *shape*
    but we blend with the diurnal curve to avoid "Normal at 3AM" producing
    the same flow as "Normal at 5PM".

    For unobserved edges: baseline V/C × diurnal curve.

    Returns: (targets dict, time-of-day factor)
    """
    tod = DIURNAL[hour]

    targets = {}
    for u, v, d in G.edges(data=True):
        speed = d.get("speed", 40)
        cap = road_capacity(speed)
        cong = d.get("congestion", [0] * 24)
        has_data = any(c > 0 for c in cong)

        if has_data:
            # Observed: congestion level sets shape, diurnal scales the baseline.
            # At congestion > 0, trust the congestion signal more.
            # At congestion = 0 ("Normal"), blend heavily with diurnal.
            vc_raw = VC_MAP.get(cong[hour], 0.25)
            if cong[hour] > 0:
                # Active congestion — trust it, but apply mild diurnal dampening
                # so off-peak "Slow" is slightly less than peak "Slow"
                vc = vc_raw * (0.7 + 0.3 * tod)
            else:
                # "Normal" — could be empty highway at 3AM or light traffic at noon
                vc = vc_raw * tod
        else:
            # Unobserved: baseline capacity utilization × diurnal curve
            base_vc = 0.10 if speed < 40 else 0.18
            vc = base_vc * tod

        targets[(u, v)] = max(0.5, vc * cap)

    return targets, tod


# ── 5. LP flow balancing ──────────────────────────────────────────────

def solve_flow_lp(G, targets, interior_nodes, pems_sources, pems_sinks,
                  nav_counts, nav_auto, hour):
    """Minimize Σ w_e·|f_e − target_e| subject to conservation.

    Variables: x = [f_0…f_{E-1}, p_0…p_{E-1}, n_0…n_{E-1}, slacks...]
    where f_e = target_e + p_e − n_e,  p_e ≥ 0, n_e ≥ 0.

    Equality constraints:
      (a) Slack definition: f_e − p_e + n_e = target_e   ∀ e
      (b) Conservation: Σ_in(f) − Σ_out(f) = 0           ∀ interior nodes
      (c) PeMS soft: net_flow(n) − pp + pn = observed     ∀ ramp nodes with data
      (d) NavigateLA turning ratios (soft)
      (e) NavigateLA auto counts: Σ_in(f) − pp + pn = observed  ∀ tube count nodes
    """
    dir_edges = list(G.edges())
    E = len(dir_edges)
    edge_idx = {e: i for i, e in enumerate(dir_edges)}

    # Weights: trust congestion-observed edges more
    w = np.ones(E)
    for i, (u, v) in enumerate(dir_edges):
        d = G.edges[u, v]
        has_data = any(c > 0 for c in d.get("congestion", [0] * 24))
        w[i] = 1.0 if has_data else 0.1

    t = np.array([targets.get(e, 1.0) for e in dir_edges])

    # ── Build constraint system ──────────────────────────────────────

    # Count PeMS constraints for this hour
    pems_list = []
    for nid, hourly in pems_sources.items():
        if hour in hourly and hourly[hour] > 0:
            pems_list.append((nid, hourly[hour], +1))   # net outflow
    for nid, hourly in pems_sinks.items():
        if hour in hourly and hourly[hour] > 0:
            pems_list.append((nid, hourly[hour], -1))   # net inflow

    # NavigateLA: for observed intersections, constrain approach-level inflows
    # to match observed total inflow per approach direction
    nav_list = _build_nav_constraints(G, nav_counts, hour, dir_edges, edge_idx)

    # NavigateLA auto: tube counters measuring total through-volume at a node
    auto_list = []
    if nav_auto:
        for nid, hourly in nav_auto.items():
            if hour in hourly and hourly[hour] > 0:
                auto_list.append((nid, hourly[hour]))

    n_pems = len(pems_list)
    n_nav = len(nav_list)
    n_auto = len(auto_list)
    n_interior = len(interior_nodes)

    # Variables: f(E) + p(E) + n(E) + pems_slack(2*n_pems) + nav_slack(2*n_nav) + auto_slack(2*n_auto)
    n_vars = 3 * E + 2 * n_pems + 2 * n_nav + 2 * n_auto

    # Rows: E (slack def) + n_interior (conservation) + n_pems + n_nav + n_auto
    n_rows = E + n_interior + n_pems + n_nav + n_auto

    # ── Objective ────────────────────────────────────────────────────
    c = np.zeros(n_vars)
    c[E:2 * E] = w       # p_e weights
    c[2 * E:3 * E] = w   # n_e weights
    # Sensor weights need to be high relative to per-edge weights (1.0/0.1)
    # because there are ~1000x more edges than sensor constraints.
    # Scale by sqrt(E) to balance influence.
    sensor_scale = max(10, E ** 0.5)
    pems_w = sensor_scale * 2.0    # PeMS: highest trust (permanent loop detectors)
    nav_w = sensor_scale * 0.5     # Nav turning: moderate (manual counts, noisy)
    auto_w = sensor_scale * 1.5    # Nav auto: high trust (tube counters, machine-read)
    for k in range(n_pems):
        off = 3 * E + 2 * k
        c[off] = pems_w
        c[off + 1] = pems_w
    for k in range(n_nav):
        off = 3 * E + 2 * n_pems + 2 * k
        c[off] = nav_w
        c[off + 1] = nav_w
    for k in range(n_auto):
        off = 3 * E + 2 * n_pems + 2 * n_nav + 2 * k
        c[off] = auto_w
        c[off + 1] = auto_w

    # ── Pre-compute node → edge adjacency (avoids O(N×E) loops) ────
    node_in_edges = defaultdict(list)   # node → [(edge_idx, ...)]
    node_out_edges = defaultdict(list)
    for i, (u, v) in enumerate(dir_edges):
        node_out_edges[u].append(i)
        node_in_edges[v].append(i)

    # ── Equality matrix (built via COO for speed) ────────────────────
    # Collect (row, col, val) triplets, convert to CSC at the end
    rows_i, cols_j, vals = [], [], []
    b = np.zeros(n_rows)

    # (a) Slack definition: f_e − p_e + n_e = target_e
    for i in range(E):
        rows_i.extend([i, i, i])
        cols_j.extend([i, E + i, 2 * E + i])
        vals.extend([1.0, -1.0, 1.0])
        b[i] = t[i]

    # (b) Conservation at interior nodes: Σ f_in − Σ f_out = 0
    row = E
    for nid in sorted(interior_nodes):
        for ei in node_in_edges.get(nid, []):
            rows_i.append(row)
            cols_j.append(ei)
            vals.append(1.0)
        for ei in node_out_edges.get(nid, []):
            rows_i.append(row)
            cols_j.append(ei)
            vals.append(-1.0)
        row += 1

    # (c) PeMS: sign * (Σ f_out − Σ f_in) − pp + pn = observed
    for k, (nid, obs, sign) in enumerate(pems_list):
        for ei in node_out_edges.get(nid, []):
            rows_i.append(row)
            cols_j.append(ei)
            vals.append(sign)
        for ei in node_in_edges.get(nid, []):
            rows_i.append(row)
            cols_j.append(ei)
            vals.append(-sign)
        off = 3 * E + 2 * k
        rows_i.extend([row, row])
        cols_j.extend([off, off + 1])
        vals.extend([-1.0, 1.0])
        b[row] = obs
        row += 1

    # (d) NavigateLA turning constraints
    for k, (edges_in_approach, obs_total) in enumerate(nav_list):
        for ei in edges_in_approach:
            rows_i.append(row)
            cols_j.append(ei)
            vals.append(1.0)
        off = 3 * E + 2 * n_pems + 2 * k
        rows_i.extend([row, row])
        cols_j.extend([off, off + 1])
        vals.extend([-1.0, 1.0])
        b[row] = obs_total
        row += 1

    # (e) NavigateLA auto counts: Σ f_in(node) − pp + pn = observed_total
    # Tube counters measure total bidirectional volume passing through.
    # We constrain the sum of all inflows to match (≈ half the total each
    # direction, but since conservation ensures inflow = outflow at interior
    # nodes, constraining inflows is equivalent to total through-volume).
    for k, (nid, obs_total) in enumerate(auto_list):
        for ei in node_in_edges.get(nid, []):
            rows_i.append(row)
            cols_j.append(ei)
            vals.append(1.0)
        off = 3 * E + 2 * n_pems + 2 * n_nav + 2 * k
        rows_i.extend([row, row])
        cols_j.extend([off, off + 1])
        vals.extend([-1.0, 1.0])
        # Tube count is bidirectional total; inflow ≈ half of total
        b[row] = obs_total / 2.0
        row += 1

    A = coo_matrix((vals, (rows_i, cols_j)), shape=(n_rows, n_vars))

    # ── Bounds ───────────────────────────────────────────────────────
    bounds = [(0, None)] * n_vars

    # ── Solve ────────────────────────────────────────────────────────
    print(f"  LP: {n_vars} vars, {n_rows} constraints "
          f"({n_pems} PeMS, {n_nav} nav turning, {n_auto} nav auto)")

    res = linprog(c, A_eq=A.tocsc(), b_eq=b, bounds=bounds,
                  method="highs", options={"presolve": True, "time_limit": 120})

    if not res.success:
        print(f"  LP FAILED: {res.message}")
        return None, dir_edges, edge_idx

    flows = res.x[:E]
    print(f"  LP solved: total flow = {flows.sum():.0f}, "
          f"objective = {res.fun:.1f}")

    # ── Report sensor fit quality ──────────────────────────────────
    if n_pems > 0:
        pems_errs = []
        for k, (nid, obs, sign) in enumerate(pems_list):
            off = 3 * E + 2 * k
            slack = res.x[off] - res.x[off + 1]  # pp - pn = error
            pems_errs.append(abs(slack) / max(obs, 1) * 100)
        print(f"    PeMS fit: avg {np.mean(pems_errs):.0f}% error "
              f"(max {np.max(pems_errs):.0f}%)")
    if n_nav > 0:
        nav_errs = []
        for k, (_, obs_total) in enumerate(nav_list):
            off = 3 * E + 2 * n_pems + 2 * k
            slack = res.x[off] - res.x[off + 1]
            nav_errs.append(abs(slack) / max(obs_total, 1) * 100)
        print(f"    Nav turning fit: avg {np.mean(nav_errs):.0f}% error "
              f"(max {np.max(nav_errs):.0f}%)")
    if n_auto > 0:
        auto_errs = []
        for k, (_, obs_total) in enumerate(auto_list):
            off = 3 * E + 2 * n_pems + 2 * n_nav + 2 * k
            slack = res.x[off] - res.x[off + 1]
            auto_errs.append(abs(slack) / max(obs_total / 2, 1) * 100)
        print(f"    Nav auto fit: avg {np.mean(auto_errs):.0f}% error "
              f"(max {np.max(auto_errs):.0f}%)")

    return flows, dir_edges, edge_idx


def _build_nav_constraints(G, nav_counts, hour, dir_edges, edge_idx):
    """Build NavigateLA turning-ratio constraints.

    For each observed intersection-approach pair, we identify the incoming
    edges from that compass direction and constrain their total to match
    the observed total inflow for that approach.

    Returns: [(list_of_edge_indices, observed_total), ...]
    """
    if not nav_counts:
        return []

    constraints = []

    for nid, hours_data in nav_counts.items():
        if hour not in hours_data:
            continue
        approaches = hours_data[hour]

        # Get incoming directed edges to this node
        in_edges = [(u, nid) for u in G.predecessors(nid)]
        if not in_edges:
            continue

        # Compute bearing of each incoming edge (from predecessor to node)
        node_lat = G.nodes[nid]["lat"]
        node_lon = G.nodes[nid]["lon"]
        edge_bearings = {}
        for u, v in in_edges:
            ei = edge_idx.get((u, v))
            if ei is None:
                continue
            u_lat = G.nodes[u]["lat"]
            u_lon = G.nodes[u]["lon"]
            bearing = _bearing(u_lat, u_lon, node_lat, node_lon)
            edge_bearings[ei] = bearing

        if not edge_bearings:
            continue

        # Map approach labels to bearing ranges
        # NB approach = traffic coming from south (bearing ~0°/360°)
        # SB approach = traffic coming from north (bearing ~180°)
        # EB approach = traffic coming from west (bearing ~90°)
        # WB approach = traffic coming from east (bearing ~270°)
        approach_bearing = {"NB": 0, "SB": 180, "EB": 90, "WB": 270}

        for appr, counts in approaches.items():
            if appr not in approach_bearing:
                continue
            target_bearing = approach_bearing[appr]
            obs_total = counts.get("total", 0)
            if obs_total <= 0:
                continue

            # Skip very low counts — they're noise-dominated and
            # create outsized % errors in the LP
            if obs_total < 20:
                continue

            # Find edges whose bearing is closest to this approach
            matched_edges = []
            for ei, brg in edge_bearings.items():
                diff = abs(((brg - target_bearing) + 180) % 360 - 180)
                if diff < 60:  # within 60° of approach direction
                    matched_edges.append(ei)

            if matched_edges:
                constraints.append((matched_edges, float(obs_total)))

    return constraints


def _bearing(lat1, lon1, lat2, lon2):
    """Bearing in degrees from (lat1,lon1) to (lat2,lon2)."""
    dlon = math.radians(lon2 - lon1)
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2)
    y = (math.cos(lat1) * math.sin(lat2) -
         math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ── 6. Flow decomposition ────────────────────────────────────────────

def decompose_routes(G, flows, dir_edges, edge_idx, max_routes=2000):
    """Decompose LP edge flows into routes.

    Two-phase approach:
    1. Path peeling: extract long source-to-sink routes (main corridors)
    2. Flow sampling: generate shorter routes from residual flow (coverage)

    This ensures both major corridors and local streets get particles.
    """
    E = len(dir_edges)
    residual = flows.copy()
    rng = np.random.default_rng(42)

    # Build adjacency for fast lookup
    out_edges = defaultdict(list)
    for i, (u, v) in enumerate(dir_edges):
        out_edges[u].append((v, i))

    # Net generation: out − in per node
    net = defaultdict(float)
    for i, (u, v) in enumerate(dir_edges):
        net[u] += flows[i]   # outflow
        net[v] -= flows[i]   # inflow

    source_excess = {}
    sink_excess = {}
    for n, g in net.items():
        if g > 0.5:
            source_excess[n] = g
        elif g < -0.5:
            sink_excess[n] = -g

    total_src = sum(source_excess.values()) if source_excess else 0
    total_sink = sum(sink_excess.values()) if sink_excess else 0
    print(f"  Sources: {len(source_excess)} nodes, {total_src:.0f} total excess")
    print(f"  Sinks:   {len(sink_excess)} nodes, {total_sink:.0f} total excess")

    routes = []

    # ── Phase 1: Path peeling (main corridors, ~40% of budget) ───────
    main_budget = max(50, max_routes * 2 // 5)
    min_push = 1.0

    for _ in range(main_budget * 5):
        if len(routes) >= main_budget:
            break
        if not source_excess:
            break

        src = max(source_excess, key=source_excess.get)
        if source_excess[src] < min_push:
            break

        path = _greedy_path(src, sink_excess, residual, out_edges)
        if path is None:
            del source_excess[src]
            continue

        path_eids = []
        for j in range(len(path) - 1):
            ei = edge_idx.get((path[j], path[j + 1]))
            if ei is not None:
                path_eids.append(ei)

        if not path_eids:
            del source_excess[src]
            continue

        sink = path[-1]
        bottleneck = min(residual[ei] for ei in path_eids)
        bottleneck = min(bottleneck, source_excess.get(src, 0))
        bottleneck = min(bottleneck, sink_excess.get(sink, 0))
        bottleneck = min(bottleneck, 50.0)
        push = max(min_push, bottleneck)

        for ei in path_eids:
            residual[ei] = max(0, residual[ei] - push)
        source_excess[src] -= push
        if source_excess[src] < min_push:
            del source_excess[src]
        sink_excess[sink] -= push
        if sink_excess[sink] < min_push:
            del sink_excess[sink]

        edge_seq = []
        for j in range(len(path) - 1):
            d = G.edges[path[j], path[j + 1]]
            edge_seq.append((d["eid"], d["dir"]))
        routes.append(dict(edges=edge_seq, count=max(1, round(push))))

    main_count = len(routes)
    main_veh = sum(r["count"] for r in routes)

    # ── Phase 2: Flow sampling (network coverage, ~60% of budget) ────
    # Sample routes proportional to residual edge flow to cover
    # arterials and local streets the main decomposition missed.
    sample_budget = max_routes - len(routes)

    # Build edge weights from residual flow
    edge_weights = residual.copy()
    edge_weights[edge_weights < 0.5] = 0
    total_residual = edge_weights.sum()

    if total_residual > 0 and sample_budget > 0:
        # Normalize to probability distribution
        edge_probs = edge_weights / total_residual

        for _ in range(sample_budget * 3):
            if len(routes) >= max_routes:
                break

            # Pick a starting edge weighted by residual flow
            start_ei = rng.choice(E, p=edge_probs)
            start_u, start_v = dir_edges[start_ei]

            # Trace a path from start_u following flow
            path_len = rng.integers(5, 25)
            path = [start_u]
            visited = {start_u}
            current = start_u

            for _ in range(path_len):
                candidates = []
                weights = []
                for v, ei in out_edges[current]:
                    if v not in visited and residual[ei] > 0.5:
                        candidates.append((v, ei))
                        weights.append(residual[ei])

                if not candidates:
                    break

                w = np.array(weights)
                w /= w.sum()
                idx = rng.choice(len(candidates), p=w)
                v, ei = candidates[idx]
                path.append(v)
                visited.add(v)
                current = v

            if len(path) < 3:
                continue

            edge_seq = []
            for j in range(len(path) - 1):
                key = (path[j], path[j + 1])
                if key in edge_idx:
                    d = G.edges[path[j], path[j + 1]]
                    edge_seq.append((d["eid"], d["dir"]))

            if len(edge_seq) >= 2:
                routes.append(dict(edges=edge_seq, count=1))

    sample_count = len(routes) - main_count
    total_veh = sum(r["count"] for r in routes)
    print(f"  Decomposed: {main_count} main + {sample_count} sampled = "
          f"{len(routes)} routes, {total_veh} vehicles")
    return routes


def _greedy_path(source, sink_excess, residual, out_edges, max_len=300):
    """Follow highest-residual edges from source to a sink."""
    path = [source]
    visited = {source}
    current = source

    for _ in range(max_len):
        best_v, best_flow = None, 0
        for v, ei in out_edges[current]:
            if v not in visited and residual[ei] > best_flow:
                best_v = v
                best_flow = residual[ei]

        if best_v is None or best_flow < 0.5:
            return None

        path.append(best_v)
        visited.add(best_v)

        if best_v in sink_excess and sink_excess[best_v] > 0.5:
            return path

        current = best_v

    return None


# ── 7. Build output ──────────────────────────────────────────────────

def format_routes(data, all_hour_routes, max_routes):
    """Convert hour-keyed routes into flat list with departure times.

    Allocates route budget proportionally to each hour's traffic volume
    to maintain realistic day/night variation in the simulator.
    """
    rng = np.random.default_rng(123)

    # Calculate total vehicles per hour from decomposition
    hour_vehicles = {}
    for hour, routes in all_hour_routes.items():
        hour_vehicles[hour] = sum(r["count"] for r in routes)
    total_veh = sum(hour_vehicles.values()) or 1

    # Allocate max_routes proportionally to each hour's vehicle count
    hour_budgets = {}
    remaining = max_routes
    hours_sorted = sorted(all_hour_routes.keys())
    for i, hour in enumerate(hours_sorted):
        if i == len(hours_sorted) - 1:
            hour_budgets[hour] = remaining
        else:
            share = hour_vehicles[hour] / total_veh
            hour_budgets[hour] = max(20, int(max_routes * share))
            remaining -= hour_budgets[hour]

    out = []
    for hour in hours_sorted:
        budget = hour_budgets[hour]
        routes = all_hour_routes[hour]
        if not routes:
            continue

        hour_out = []
        for route in routes:
            eids = [eid for eid, _ in route["edges"]]
            dirs = [d for _, d in route["edges"]]
            count = route["count"]

            for _ in range(min(count, 50)):
                hour_out.append(dict(
                    edges=eids,
                    dirs=dirs,
                    depart=round(hour + rng.uniform(0, 1), 3),
                    count=1,
                ))

        # Trim to budget for this hour
        if len(hour_out) > budget:
            rng.shuffle(hour_out)
            hour_out = hour_out[:budget]
        out.extend(hour_out)

    data["routes"] = out
    return data


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build flow-balanced routes")
    parser.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--hour", type=int, default=None)
    parser.add_argument("--all-hours", action="store_true")
    parser.add_argument("--max-routes", type=int, default=15000)
    args = parser.parse_args()

    if args.output is None:
        args.output = args.graph

    t0 = time.time()

    # ── Load graph ───────────────────────────────────────────────────
    print(f"Loading graph from {args.graph}...")
    G, data = load_graph(args.graph)
    print(f"  {G.number_of_nodes()} nodes, {G.number_of_edges()} directed edges")

    # ── Match sensors ────────────────────────────────────────────────
    print("Matching sensors...")
    tree, node_ids = build_kdtree(G)
    nav_counts = match_navigatela(G, tree, node_ids)
    nav_auto = match_navigatela_auto(G, tree, node_ids)
    pems_sources, pems_sinks = match_pems(G, tree, node_ids)

    # ── Classify nodes ───────────────────────────────────────────────
    print("Classifying nodes...")
    interior, boundary, free = classify_nodes(G, pems_sources, pems_sinks)

    # ── Choose hours ─────────────────────────────────────────────────
    if args.all_hours:
        hours = list(range(24))
    elif args.hour is not None:
        hours = [args.hour]
    else:
        hours = [0, 3, 6, 7, 8, 9, 12, 15, 16, 17, 18, 21]

    # ── Solve per hour ───────────────────────────────────────────────
    all_routes = {}

    # Allocate routes proportional to diurnal traffic volume
    diurnal_sum = sum(DIURNAL[h] for h in hours)
    per_hour_cap = {}
    for h in hours:
        share = DIURNAL[h] / diurnal_sum if diurnal_sum > 0 else 1.0 / len(hours)
        per_hour_cap[h] = max(200, int(args.max_routes * share * 1.2))  # 1.2x headroom
    print(f"Route budget: {', '.join(f'{h}h={per_hour_cap[h]}' for h in hours[:6])}...")

    for hour in hours:
        print(f"\n{'=' * 50}")
        print(f"  Hour {hour:02d}:00")
        print(f"{'=' * 50}")

        targets, tod = compute_targets(G, hour)
        target_total = sum(targets.values())
        print(f"  ToD factor: {tod:.3f}, target total: {target_total:.0f}")

        flows, dir_edges, eidx = solve_flow_lp(
            G, targets, interior, pems_sources, pems_sinks,
            nav_counts, nav_auto, hour)

        if flows is None:
            all_routes[hour] = []
            continue

        routes = decompose_routes(G, flows, dir_edges, eidx,
                                  max_routes=per_hour_cap[hour])
        all_routes[hour] = routes

    # ── Output ───────────────────────────────────────────────────────
    print(f"\nBuilding output...")
    output = format_routes(data, all_routes, args.max_routes)

    out_json = json.dumps(output, separators=(",", ":"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(out_json)

    elapsed = time.time() - t0
    n = len(output.get("routes", []))
    print(f"\nWrote {args.output} ({len(out_json) / 1024:.0f} KB)")
    print(f"  {n} routes across {len(hours)} hours")
    print(f"  Elapsed: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
