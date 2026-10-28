#!/usr/bin/env python3
"""Do optimized search loops beat typical circling inside the simulation itself?

The optimizer plans with per-block chances from the weekly export and treats blocks as independent. Here probe
drivers follow the planned loops minute by minute in a fresh simulated day (different seed), competing with all
other drivers for the same spaces: on each block they take a legal free space on a usable face within the walk
limit, else drive on; at the end of the loop they go to the best lot. Same for typical circling.

Usage: python3 scripts/parking_sim/evaluate_routes.py [--dests 30] [--probes 6]
"""
import argparse
import math
import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
os.environ.setdefault("PARKSIM_SEED", "11")          # a different day from the export's seeds 1-4
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[0] / "curb"))
import sim as simmod  # noqa: E402
from common import POPULAR_TIMES_DB, xy  # noqa: E402
from optimize import DAYS, DETOUR, Planner  # noqa: E402
from scipy.spatial import cKDTree  # noqa: E402
from supply import OUT_DIR  # noqa: E402

SCENARIOS = [("Wed", "12:00"), ("Wed", "19:00"), ("Fri", "21:00"), ("Sat", "13:00"), ("Sat", "21:00")]


def destinations(S, k):
    db = sqlite3.connect(f"file:{POPULAR_TIMES_DB}?mode=ro", uri=True)
    b = S["bbox"]
    rows = db.execute("select lat, lng from places where reviews >= 200 and lat between ? and ? and lng between ? and ?",
                      (b[0], b[1], b[2], b[3])).fetchall()
    rng = np.random.default_rng(5)
    pick = rng.choice(len(rows), size=min(k, len(rows)), replace=False)
    return [rows[i] for i in pick]


def probe_for(pl, route, terms, G, dest, start_min, walk_max, stay_min=90):
    p, walk, tt = terms
    faces, turns = [], []
    for i, e in enumerate(route):
        fs = []
        for side in ("face_right", "face_left"):
            f = int(pl.N[side][e])
            if f >= 0:
                d = float(np.hypot(*(pl.face_xy[f] - dest))) * DETOUR
                if d <= walk_max:
                    fs.append((f, d))
        faces.append(sorted(fs, key=lambda x: x[1]))
        turns.append(pl.turn_s(e, route[i + 1]) if i + 1 < len(route) else 0.0)
    end = pl.N["dst"][route[-1]] if route else None
    return dict(start=start_min, route=list(route), faces=faces, tt=[float(tt[e]) for e in route], turn=turns,
                fallback_s=float(G[end]) if end is not None else 0.0, stay_min=stay_min)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dests", type=int, default=30)
    ap.add_argument("--probes", type=int, default=6)
    ap.add_argument("--walk", type=float, default=400)
    args = ap.parse_args()
    pl = Planner()
    S = dict(np.load(OUT_DIR / "supply.npz"))
    dests = destinations(S, args.dests)
    node_tree = cKDTree(pl.node_xy)
    rows = []
    for day_name in dict.fromkeys(d for d, _ in SCENARIOS):
        day = DAYS.index(day_name)
        probes, meta = [], []
        for dn, t in SCENARIOS:
            if dn != day_name:
                continue
            hh, mm = map(int, t.split(":"))
            slot, t0 = (hh * 60 + mm) // 15, hh * 60 + mm
            t = f"{dn} {t}"   # scenario key: day + time
            for di, (lat, lon) in enumerate(dests):
                dest = xy(lat, lon)[0]
                start = int(node_tree.query(dest)[1])
                terms = pl.edge_terms(dest, day, slot, args.walk)
                G, _ = pl.lot_costs(dest, day, slot, args.walk)
                _, opt = pl.optimize(start, terms, G, 600)
                base = pl.baseline(start, dest, 600, terms)
                pred = {s: pl.evaluate(r, terms, G, start)[0] for s, r in (("optimized", opt), ("circling", base))}
                for k in range(args.probes):
                    sm = t0 + k * 7
                    for strat, r in (("optimized", opt), ("circling", base)):
                        if not r:   # optimizer says: go straight to the lot
                            rows.append((t, strat, False, float(G[start]), pred[strat], di, k))
                            continue
                        probes.append(probe_for(pl, r, terms, G, dest, sm, args.walk))
                        meta.append((t, strat, pred[strat], di, k))
        D = simmod.prepare_day(S, day, "night", anchor_train_all=True, verbose=False)
        sim = simmod.Sim(S, day, 2)
        sim.run(D, probes=probes)
        for k, (t, strat, pr, di, kk) in enumerate(meta):
            ok, secs, _ = sim.probe_results.get(k, (False, np.nan, np.nan))
            rows.append((t, strat, ok, secs, pr, di, kk))
        print(f"  simulated {day_name} with {len(probes)} probe drivers", flush=True)
    R = np.array(rows, dtype=object)
    print(f"\n{len(dests)} destinations x {args.probes} start times, walk <= {args.walk:.0f} m; minutes to the door (mean)")
    print(f"  {'scenario':12s} {'optimized':>22s} {'typical circling':>24s}   predicted opt / circ")
    for dn, tm in SCENARIOS:
        t = f"{dn} {tm}"
        line = []
        for strat in ("optimized", "circling"):
            m = (R[:, 0] == t) & (R[:, 1] == strat)
            secs = np.array(R[m, 3], float)
            ok = np.array(R[m, 2], bool)
            line.append((np.nanmean(secs) / 60, ok.mean(), np.nanmean(np.array(R[m, 4], float)) / 60))
        key = t
        print(f"  {key:12s} {line[0][0]:6.1f} min ({line[0][1]:4.0%} street) {line[1][0]:8.1f} min ({line[1][1]:4.0%} street)"
              f"   {line[0][2]:5.1f} / {line[1][2]:5.1f}")
    by = {(r[0], r[5], r[6], r[1]): float(r[3]) for r in rows}
    pairs = np.array([(v, by[(t, di, k, "circling")]) for (t, di, k, s), v in by.items()
                      if s == "optimized" and (t, di, k, "circling") in by])
    pairs = pairs[np.isfinite(pairs).all(1)]
    print(f"  overall: optimized {pairs[:, 0].mean() / 60:.1f} min vs circling {pairs[:, 1].mean() / 60:.1f} min over {len(pairs)} paired trips;"
          f" optimized faster or equal in {np.mean(pairs[:, 0] <= pairs[:, 1] + 1e-6):.0%}")


if __name__ == "__main__":
    main()
