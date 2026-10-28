#!/usr/bin/env python3
"""Export a week of block-face contention for the search-path optimizer.

Runs the simulator for Mon-Sun (one week-of-month for sweeping), averaging several random seeds per day so single
faces are stable, and writes data/parking_sim/contention_week.json.gz:
  faces: [{id, block, side, lat, lon, capacity, cls, line}]   (core area only; line = block centerline [[lat, lon], ...];
         side L/R is relative to the line's direction; lat/lon is the middle of the face's parking lane)
  p_free[day][face] -> 96 ints, percent of minutes in each 15-min slot with a free legal space for a visitor
  occ[day][face]    -> 96 ints, mean percent of capacity occupied
Usage: python3 scripts/parking_sim/export.py [--week 2] [--anchor-train-all]
"""
import argparse
import gzip
import json
import os
import subprocess
import tempfile
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from supply import OUT_DIR  # noqa: E402

CLASS_NAMES = ("residential", "unclassified", "living_street", "tertiary", "secondary", "primary")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", type=int, default=2)
    ap.add_argument("--anchor-train-all", action="store_true")
    ap.add_argument("--anchor", default="night", choices=["night", "midday", "none"])
    ap.add_argument("--seeds", type=int, default=4)
    args = ap.parse_args()
    S = dict(np.load(OUT_DIR / "supply.npz"))
    keep = np.where(S["core"] & (S["capacity"] >= 0.5))[0]
    ends = list(S["poly_start"][1:]) + [len(S["poly"])]
    faces = [dict(id=int(i), block=int(S["edge"][i]), side=str(S["side"][i]), lat=round(float(S["latlon"][i, 0]), 6),
                  lon=round(float(S["latlon"][i, 1]), 6), capacity=int(round(S["capacity"][i])), cls=CLASS_NAMES[S["cls"][i]],
                  line=S["poly"][S["poly_start"][i]:ends[i]].tolist())
             for i in keep]
    out = dict(faces=faces, slot_minutes=15, days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], week_of_month=args.week,
               p_free=[], occ=[])
    tmp = Path(tempfile.mkdtemp(prefix="parksim_"))
    base = [sys.executable, str(HERE / "sim.py"), "--week", str(args.week), "--anchor", args.anchor]
    if args.anchor_train_all:
        base.append("--anchor-train-all")
    env = lambda seed: dict(os.environ, PARKSIM_SEED=str(seed), PARKSIM_OUT=str(tmp / f"s{seed}"))
    for seed in range(1, args.seeds + 1):
        (tmp / f"s{seed}").mkdir()
    subprocess.run(base + ["--weekday", "2"], env=env(1), check=True, stdout=subprocess.DEVNULL)  # builds the anchor cache
    jobs = [(wd, seed) for wd in range(7) for seed in range(1, args.seeds + 1) if (wd, seed) != (2, 1)]
    for i in range(0, len(jobs), 10):  # 10 days at a time
        procs = [subprocess.Popen(base + ["--weekday", str(wd)], env=env(seed), stdout=subprocess.DEVNULL) for wd, seed in jobs[i:i + 10]]
        if any(p.wait() for p in procs):
            raise RuntimeError("simulation failed")
    for wd in range(7):
        R = [np.load(tmp / f"s{seed}" / f"contention_wd{wd}_w{args.week}.npz") for seed in range(1, args.seeds + 1)]
        cap = np.maximum(R[0]["capacity"][keep], 1)[:, None]
        p_free = np.mean([r["p_free"][keep] for r in R], axis=0)
        occ = np.mean([r["occ"][keep] for r in R], axis=0)
        out["p_free"].append(np.rint(p_free * 100).astype(int).tolist())
        out["occ"].append(np.rint(np.clip(occ / cap, 0, 1) * 100).astype(int).tolist())
    dest = OUT_DIR / "contention_week.json.gz"
    with gzip.open(dest, "wt") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{len(faces)} faces x 7 days x 96 slots, {args.seeds} seeds per day -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
