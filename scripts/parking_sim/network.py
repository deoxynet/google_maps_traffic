#!/usr/bin/env python3
"""Directed driving graph over the simulator's blocks, for the parking-search optimizer.

Nodes are block endpoints (blocks from supply.npz are chained between intersections and split at <= 200 m, so split
points become degree-2 nodes). One-way and street names come from OSM ways (data/parking_sim/osm_ways.json, from
examples/export_street_ways.rs), matched to each block by position and direction.

Each directed edge knows which curb faces a driver can take while driving it: the right-hand face on a two-way
street, both faces on a one-way street (California allows parking on the left side of a one-way road).
Drive time = length / class speed, slowed by Google traffic history (block's mean congestion level by weekday or
weekend and hour, from traffic_z15.npz) where available.

Usage: python3 scripts/parking_sim/network.py   -> data/parking_sim/network.npz
"""
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "curb"))
from common import CLASSES, xy  # noqa: E402
from supply import OUT_DIR  # noqa: E402

# free-flow driving speed incl. stop signs / signals, km/h, by class
SPEED_KPH = {"residential": 22, "unclassified": 22, "living_street": 12, "tertiary": 28, "secondary": 32, "primary": 35}
CONGESTION_FACTOR = np.array([1.0, 0.6, 0.35, 0.2])   # Google level 0 normal .. 3 severe


def oneway_of(tags, highway):
    v = tags.get("oneway", "")
    if v in ("yes", "1", "true") or tags.get("junction") == "roundabout" or (highway.startswith("motorway") and v != "no"):
        return 1
    if v == "-1":
        return -1
    return 0


def main():
    S = np.load(OUT_DIR / "supply.npz")
    left = np.where(S["side"] == "L")[0]
    right_of = {S["edge"][f]: f for f in np.where(S["side"] == "R")[0]}
    ends = list(S["poly_start"][1:]) + [len(S["poly"])]
    # block geometry (centerline, lat/lon) and endpoints -> nodes
    node_id, node_ll = {}, []

    def nid(lat, lon):
        k = (round(lat, 6), round(lon, 6))
        if k not in node_id:
            node_id[k] = len(node_id)
            node_ll.append(k)
        return node_id[k]

    blocks = []
    for f in left:
        P = S["poly"][S["poly_start"][f]:ends[f]]
        blocks.append(dict(block=int(S["edge"][f]), faceL=int(f), faceR=int(right_of.get(S["edge"][f], -1)),
                           u=nid(*P[0]), v=nid(*P[-1]), line=P, cls=int(S["cls"][f]), length=float(S["length_m"][f])))
    # OSM ways -> sample points with direction, way index
    W = json.load(open(OUT_DIR / "osm_ways.json"))["ways"]
    pts, dirs, owner = [], [], []
    for i, w in enumerate(W):
        ll = np.array([[n[1], n[2]] for n in w["nodes"]])
        m = xy(ll[:, 0], ll[:, 1])
        for a, b in zip(m[:-1], m[1:]):
            L = np.hypot(*(b - a))
            if L < 1e-6:
                continue
            for t in np.linspace(0, 1, max(1, int(L // 4)), endpoint=False):
                pts.append(a + (b - a) * t)
                dirs.append(math.atan2(b[1] - a[1], b[0] - a[0]))
                owner.append(i)
    tree = cKDTree(np.array(pts))
    dirs, owner = np.array(dirs), np.array(owner)
    unmatched = 0
    for bl in blocks:
        m = xy(bl["line"][:, 0], bl["line"][:, 1])
        k = len(m) // 2
        a, b = (m[k - 1], m[k]) if k > 0 else (m[0], m[1])
        mid, bdir = (a + b) / 2, math.atan2(b[1] - a[1], b[0] - a[0])
        best = None
        for j in tree.query_ball_point(mid, 8.0):
            diff = abs((dirs[j] - bdir + math.pi) % (2 * math.pi) - math.pi)   # 0 = same way, pi = opposite
            ang = min(diff, math.pi - diff)
            if ang < math.radians(25) and (best is None or ang < best[0]):
                best = (ang, owner[j], diff < math.pi / 2)
        if best is None:
            unmatched += 1
            bl.update(oneway=0, name="")
            continue
        w = W[best[1]]
        ow = oneway_of(w["tags"], w["highway"])
        bl.update(oneway=ow if best[2] else -ow, name=w["tags"].get("name", ""))
    # traffic: block congestion by (weekend, hour) where the history covers it
    slow = None
    tz = OUT_DIR / "traffic_z15.npz"
    if tz.exists():
        Z = np.load(tz)
        LA = ZoneInfo("America/Los_Angeles")
        t = [datetime.fromtimestamp(int(s), LA) for s in Z["timestamp"] if s > 0]
        lev = Z["level"][Z["timestamp"] > 0]
        wk = np.array([x.weekday() >= 5 for x in t]).astype(int)
        hr = np.array([x.hour for x in t])
        slow = np.full((lev.shape[1], 2, 24), np.nan, np.float32)
        for we in (0, 1):
            for h in range(24):
                sel = (wk == we) & (hr == h)
                if sel.sum() >= 3:
                    with np.errstate(invalid="ignore"):
                        slow[:, we, h] = np.nanmean(lev[sel], 0)
    # directed edges
    E = []
    for bl in blocks:
        for forward in (True, False):
            if (forward and bl["oneway"] == -1) or (not forward and bl["oneway"] == 1):
                continue
            right = bl["faceR"] if forward else bl["faceL"]
            leftf = (bl["faceL"] if forward else bl["faceR"]) if bl["oneway"] != 0 else -1
            E.append((bl["u"], bl["v"], bl["block"], forward, right, leftf) if forward else
                     (bl["v"], bl["u"], bl["block"], forward, right, leftf))
    E = np.array(E, dtype=object)
    blk_index = {bl["block"]: i for i, bl in enumerate(blocks)}
    length = np.array([blocks[blk_index[e[2]]]["length"] for e in E])
    cls = np.array([blocks[blk_index[e[2]]]["cls"] for e in E])
    base_s = length / (np.array([SPEED_KPH[CLASSES[c]] for c in cls]) / 3.6)
    tt = np.repeat(base_s[:, None, None], 2, 1).repeat(24, 2).astype(np.float32)   # [edge, weekend, hour] seconds
    if slow is not None:
        lv = slow[np.array([e[2] for e in E])]
        f = np.where(np.isfinite(lv), np.interp(np.nan_to_num(lv), [0, 1, 2, 3], CONGESTION_FACTOR), 1.0)
        tt = tt / f
    names = np.array([blocks[blk_index[e[2]]]["name"] for e in E])
    lines = [blocks[blk_index[e[2]]]["line"] if e[3] else blocks[blk_index[e[2]]]["line"][::-1] for e in E]
    np.savez_compressed(OUT_DIR / "network.npz", node_ll=np.array(node_ll), src=E[:, 0].astype(int), dst=E[:, 1].astype(int),
                        block=E[:, 2].astype(int), forward=E[:, 3].astype(bool), face_right=E[:, 4].astype(int),
                        face_left=E[:, 5].astype(int), length=length, cls=cls, travel_s=tt, name=names,
                        line_start=np.cumsum([0] + [len(x) for x in lines])[:-1], line=np.vstack(lines))
    ow = sum(1 for b in blocks if b["oneway"] != 0)
    print(f"{len(blocks)} blocks -> {len(node_ll)} nodes, {len(E)} directed edges; one-way blocks {ow};"
          f" unmatched to OSM {unmatched}; traffic-adjusted edges {int(np.isfinite(slow).any((1, 2)).sum()) if slow is not None else 0}")


if __name__ == "__main__":
    main()
