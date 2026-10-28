#!/usr/bin/env python3
"""Parking-search route optimizer: the driving loop with the lowest expected time from arrival to the door.

Model (the probability-aware search of Hickert et al. 2026, on curb faces instead of lots):
  * driving a directed block offers the curb faces the driver can legally take (right side; both on one-way streets)
  * p(block) = 1 - prod over usable faces within the walk limit of (1 - p_face), where p_face is the simulator's
    capture rate: the share of simulated searchers who checked that face in that 15-minute slot and got a space
    (falls back to the share of time the face had a free space when too few searchers checked it). A block already
    passed on this loop counts at 35% (its spaces were just seen full)
  * parking on a block costs: drive time to its middle + 30 s to park + walk to the destination at 1.3 m/s
  * never parking within the loop costs: loop time + the best public lot from where the loop ends (driving time
    over the street graph + 2 min to get in and pay + walk; unverified OSM garages +2 min), from lots.json
Beam search over loops up to --max-drive seconds. The baseline is a driver who heads for the destination and keeps
circling as close to it as possible.

Usage: python3 scripts/parking_sim/optimize.py --dest 34.0637,-118.3090 --day Fri --time 20:00 [--walk 400] [--stay 2]
"""
import argparse
import gzip
import heapq
from collections import defaultdict
import json
import math
import sys
from pathlib import Path
from urllib.parse import quote

import numpy as np
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "curb"))
from common import xy  # noqa: E402
from supply import OUT_DIR  # noqa: E402

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
PARK_S, WALK_MPS, DETOUR = 30.0, 1.3, 1.25
REVISIT = 0.35
LOT_ENTRY_S = 120.0
UNVERIFIED_S = 300.0      # an OSM garage with no access tag may be tenant-only: about a coin flip of driving on
FAR_FALLBACK_S = 900.0    # no public lot in range: keep searching farther out


class Planner:
    def __init__(self):
        N = np.load(OUT_DIR / "network.npz")
        self.N = {k: N[k] for k in N.files}
        S = np.load(OUT_DIR / "supply.npz")
        self.face_xy = S["xy"]
        Ex = json.load(gzip.open(OUT_DIR / "contention_week.json.gz", "rt"))
        self.p_free = np.zeros((len(S["xy"]), 7, 96), np.float32)   # faces without parking stay 0
        ids = np.array([f["id"] for f in Ex["faces"]])
        pf = np.array(Ex["p_free"], np.float32).transpose(1, 0, 2) / 100
        if "capture" in Ex:
            cr = np.array(Ex["capture"], np.float32).transpose(1, 0, 2)
            pf = np.where(cr >= 0, cr / 100, pf)
        self.p_free[ids] = pf
        self.node_xy = xy(self.N["node_ll"][:, 0], self.N["node_ll"][:, 1])
        self.out = [[] for _ in range(len(self.node_xy))]
        for e, s in enumerate(self.N["src"]):
            self.out[s].append(e)
        ls = self.N["line_start"]
        le = list(ls[1:]) + [len(self.N["line"])]
        self.lines = [self.N["line"][a:b] for a, b in zip(ls, le)]
        self.mid = np.array([xy(l[len(l) // 2:len(l) // 2 + 1, 0], l[len(l) // 2:len(l) // 2 + 1, 1])[0] for l in self.lines])
        self.lots = json.load(open(OUT_DIR / "lots.json"))
        self.lot_xy = xy([x["lat"] for x in self.lots], [x["lon"] for x in self.lots])
        self.lot_node = cKDTree(self.node_xy).query(self.lot_xy)[1]
        self.into = defaultdict(list)
        for e, d in enumerate(self.N["dst"]):
            self.into[d].append(e)
        self.heading = np.array([math.atan2(*(lambda m: (m[-1, 1] - m[0, 1], m[-1, 0] - m[0, 0]))(xy(l[:, 0], l[:, 1]))) for l in self.lines])

    def edge_terms(self, dest, day, slot, walk_max):
        """Per directed edge: chance of a usable free space, expected walk, drive time."""
        p = np.zeros(len(self.N["src"]))
        walk = np.zeros(len(self.N["src"]))
        for side in ("face_right", "face_left"):
            f = self.N[side]
            ok = f >= 0
            d = np.full(len(f), np.inf)
            d[ok] = np.hypot(*(self.face_xy[f[ok]] - dest).T) * DETOUR
            pf = np.where(ok & (d <= walk_max), self.p_free[np.maximum(f, 0), day, slot], 0.0)
            dd = np.where(pf > 0, d, 0.0)
            walk = np.where(pf > 0, (walk * p + dd * pf * (1 - p)) / np.maximum(p + pf * (1 - p), 1e-9), walk)
            p = 1 - (1 - p) * (1 - pf)
        tt = self.N["travel_s"][:, int(day >= 5), slot // 4].astype(float)
        return p, walk, tt

    def lot_costs(self, dest, day, slot, walk_max):
        """For every node: seconds to the best open public lot (drive + entry + walk) and which lot."""
        tt = self.N["travel_s"][:, int(day >= 5), slot // 4].astype(float)
        n = len(self.node_xy)
        best, which = np.full(n, np.inf), np.full(n, -1)
        for i, lot in enumerate(self.lots):
            walk = np.hypot(*(self.lot_xy[i] - dest)) * DETOUR
            if walk > max(walk_max, 800) or (lot["open"] and not lot["open"][day][slot // 4]):
                continue
            extra = LOT_ENTRY_S + walk / WALK_MPS + (0.0 if lot.get("verified", True) else UNVERIFIED_S)
            dist = np.full(n, np.inf)
            dist[self.lot_node[i]] = 0.0
            pq = [(0.0, self.lot_node[i])]
            while pq:  # reverse Dijkstra: time from every node to this lot
                d, v = heapq.heappop(pq)
                if d > dist[v]:
                    continue
                for e in self.into[v]:
                    u = self.N["src"][e]
                    if d + tt[e] < dist[u]:
                        dist[u] = d + tt[e]
                        heapq.heappush(pq, (dist[u], u))
            cost = dist + extra
            better = cost < best
            best[better], which[better] = cost[better], i
        far = best > FAR_FALLBACK_S
        best[far], which[far] = FAR_FALLBACK_S, -1
        return best, which

    def evaluate(self, route, terms, fallback, start=None):
        if not np.isscalar(fallback):
            node = self.N["dst"][route[-1]] if route else start
            fallback = fallback[node]
        p, walk, tt = terms
        E, S, T, seen = 0.0, 1.0, 0.0, set()
        for e in route:
            pe = p[e] * (REVISIT if self.N["block"][e] in seen else 1.0)
            E += S * pe * (T + tt[e] / 2 + PARK_S + walk[e] / WALK_MPS)
            S *= 1 - pe
            T += tt[e]
            seen.add(self.N["block"][e])
        return E + S * (T + fallback), 1 - S, T

    def turn_s(self, e1, e2):
        d = abs((self.heading[e2] - self.heading[e1] + math.pi) % (2 * math.pi) - math.pi)
        return 3.0 if d < math.radians(40) else (12.0 if d < math.radians(140) else 25.0)

    def optimize(self, start, terms, fallback, max_drive, beam=400):
        """fallback: seconds to the best lot from each node (array) or a flat cost."""
        p, walk, tt = terms
        G = (lambda v: fallback[v]) if not np.isscalar(fallback) else (lambda v: fallback)
        # state: (J, E, S, T, node, last_edge, route)
        frontier = [(G(start), 0.0, 1.0, 0.0, start, -1, ())]
        best = (G(start), ())
        while frontier:
            nxt = {}
            for J, E, S, T, node, last, route in frontier:
                seen = {self.N["block"][x] for x in route}
                for e in self.out[node]:
                    if last >= 0 and self.N["block"][e] == self.N["block"][last] and len(self.out[node]) > 1:
                        continue  # no U-turn on the same block unless it's a dead end
                    t0 = T + (self.turn_s(last, e) if last >= 0 else 0.0)
                    if t0 + tt[e] > max_drive:
                        continue
                    pe = p[e] * (REVISIT if self.N["block"][e] in seen else 1.0)
                    E2 = E + S * pe * (t0 + tt[e] / 2 + PARK_S + walk[e] / WALK_MPS)
                    S2, T2 = S * (1 - pe), t0 + tt[e]
                    J2 = E2 + S2 * (T2 + G(self.N["dst"][e]))
                    key = (self.N["dst"][e], e)
                    if key not in nxt or J2 < nxt[key][0]:
                        nxt[key] = (J2, E2, S2, T2, self.N["dst"][e], e, route + (e,))
                    if J2 < best[0]:
                        best = (J2, route + (e,))
            frontier = heapq.nsmallest(beam, nxt.values(), key=lambda s: s[0])
        return best

    def baseline(self, start, dest, max_drive, terms):
        """Typical driver: always take the next block whose middle is closest to the destination, avoiding repeats."""
        tt = terms[2]
        route, node, last, T, recent = [], start, -1, 0.0, []
        while True:
            cands = [e for e in self.out[node] if not (last >= 0 and self.N["block"][e] == self.N["block"][last] and len(self.out[node]) > 1)]
            if not cands:
                break
            cands.sort(key=lambda e: (self.N["block"][e] in recent, np.hypot(*(self.mid[e] - dest))))
            e = cands[0]
            t0 = T + (self.turn_s(last, e) if last >= 0 else 0.0)
            if t0 + tt[e] > max_drive:
                break
            route.append(e)
            recent = (recent + [self.N["block"][e]])[-6:]
            node, last, T = self.N["dst"][e], e, t0 + tt[e]
        return route

    def describe(self, route):
        out, prev = [], None
        for e in route:
            n = str(self.N["name"][e]) or "(unnamed)"
            if n != prev:
                out.append(n)
                prev = n
        return " -> ".join(out)

    def waypoints(self, route, terms, max_points):
        """Turn points that force Google Maps along the loop: the middle of each block after a turn or name change."""
        p = terms[0]
        pts, S = [], 1.0
        for i, e in enumerate(route):
            turned = i == 0 or self.turn_s(route[i - 1], e) > 3.0 or self.N["name"][e] != self.N["name"][route[i - 1]]
            if turned:
                pts.append(i)
            S *= 1 - p[e]
            if 1 - S >= 0.95:
                break
        idx = pts[:max_points + 1]
        ll = [self.lines[route[i]][len(self.lines[route[i]]) // 2] for i in idx]
        return ll

    @staticmethod
    def maps_link(points):
        fmt = lambda q: f"{q[0]:.6f},{q[1]:.6f}"
        dest = fmt(points[-1])
        wps = "|".join(fmt(q) for q in points[:-1])
        url = f"https://www.google.com/maps/dir/?api=1&destination={dest}&travelmode=driving"
        return url + (f"&waypoints={quote(wps, safe=',')}" if wps else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True, help="lat,lon")
    ap.add_argument("--day", default="Fri", choices=DAYS)
    ap.add_argument("--time", default="20:00")
    ap.add_argument("--walk", type=float, default=400, help="max walk to the destination, metres")
    ap.add_argument("--max-drive", type=float, default=600, help="longest search loop, seconds of driving")
    ap.add_argument("--stay", type=float, default=2.0, help="planned stay in hours (for lot prices)")
    args = ap.parse_args()
    lat, lon = map(float, args.dest.split(","))
    hh, mm = map(int, args.time.split(":"))
    day, slot = DAYS.index(args.day), (hh * 60 + mm) // 15
    pl = Planner()
    dest = xy(lat, lon)[0]
    start = int(cKDTree(pl.node_xy).query(dest)[1])
    plan(pl, dest, start, day, slot, args, lat, lon)
    if args.walk < 800:
        Jo, Po = plan(pl, dest, start, day, slot, args, lat, lon, quiet=True)
        args2 = argparse.Namespace(**{**vars(args), "walk": 800})
        J8, P8 = plan(pl, dest, start, day, slot, args2, lat, lon, quiet=True)
        print(f"  with up to 800 m of walking: expected {J8 / 60:.1f} min, finds a space {P8:.0%}"
              f" ({'worth it' if J8 < Jo - 30 else 'not worth it'})")


def lot_text(pl, i, dest, stay):
    lot = pl.lots[i]
    walk = np.hypot(*(pl.lot_xy[i] - dest)) * DETOUR
    if lot["rate_per_hr"] is not None:
        price = lot["rate_per_hr"] * stay
        if lot["daily_max"]:
            price = min(price, lot["daily_max"])
        price_s = f"${price:.2f} for {stay:g} h"
    else:
        price_s = "price not posted"
    flag = "" if lot.get("verified", True) else ", unverified public garage (OSM)"
    return f"{lot['name']} ({lot['operator']}), {walk:.0f} m walk, {price_s}{flag}"


def plan(pl, dest, start, day, slot, args, lat, lon, quiet=False):
    terms = pl.edge_terms(dest, day, slot, args.walk)
    G, which = pl.lot_costs(dest, day, slot, args.walk)
    J, route = pl.optimize(start, terms, G, args.max_drive)
    Jo, Po, To = pl.evaluate(route, terms, G, start)
    if quiet:
        return Jo, Po
    base = pl.baseline(start, dest, args.max_drive, terms)
    Jb, Pb, Tb = pl.evaluate(base, terms, G, start)
    print(f"{args.day} {args.time}, destination {lat:.5f},{lon:.5f}, walk <= {args.walk:.0f} m")
    if which[start] >= 0:
        print(f"  straight to a lot: {G[start] / 60:.1f} min to the door: {lot_text(pl, which[start], dest, args.stay)}")
    else:
        print("  no public lot within reach")
    if not route:
        print("  street search doesn't beat that: go straight to the lot" if which[start] >= 0 else "  no street search helps here")
        return Jo, Po
    end = pl.N["dst"][route[-1]]
    print(f"  optimized street loop: expected {Jo / 60:.1f} min to the door; finds a space within the loop {Po:.0%};"
          f" loop {To / 60:.1f} min of driving; if it fails: {lot_text(pl, which[end], dest, args.stay) if which[end] >= 0 else 'no lot'}")
    print(f"  typical circling (then the nearest lot): expected {Jb / 60:.1f} min; finds a space {Pb:.0%}; the loop saves {(Jb - Jo) / 60:.1f} min")
    print(f"  route: {pl.describe(route)}")
    desk = pl.waypoints(route, terms, 9)
    mob = pl.waypoints(route, terms, 3)
    print(f"  Google Maps ({len(desk) - 1} waypoints): {pl.maps_link(desk)}")
    print(f"  Google Maps, mobile ({len(mob) - 1} waypoints): {pl.maps_link(mob)}")
    return Jo, Po


if __name__ == "__main__":
    main()
