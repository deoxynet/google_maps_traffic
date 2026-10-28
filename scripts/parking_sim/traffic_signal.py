#!/usr/bin/env python3
"""Does Google traffic show parking search? Per-block slowdown profiles vs parking evidence.

Each block's congestion is compared with its own weekday-midday baseline (10:00-15:59), so a street that is always
slow (signals, narrow lanes) doesn't count. Two indices per block:
  commute  = weekday 7-9h and 16-18h level minus baseline  (through traffic)
  evening  = 18:00-23:59 level minus baseline              (no through traffic: destinations, circling)
Residential blocks are aggregated per 150 m cell and tested against evening permit-district tickets, night double
parking, and the simulator's evening contention.

Usage: python3 scripts/parking_sim/traffic_signal.py   (after traffic_profiles.py and export.py)
"""
import gzip
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from scipy.spatial import cKDTree
from scipy.stats import spearmanr

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "curb"))
from common import CELL_M, CURB_DIR, xy  # noqa: E402
from supply import OUT_DIR, curb_samples  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")


def block_index(level, mask, min_obs=8):
    """Mean level per block over the selected snapshots (NaN where fewer than min_obs observations)."""
    L = level[mask]
    n = np.isfinite(L).sum(0)
    with np.errstate(invalid="ignore"):
        m = np.nanmean(L, 0)
    m[n < min_obs] = np.nan
    return m, n


def main():
    Z = np.load(OUT_DIR / "traffic_z15.npz")
    level, ts = Z["level"], Z["timestamp"]
    keep = ts > 0
    level, ts = level[keep], ts[keep]
    local = [datetime.fromtimestamp(int(t), LA) for t in ts]
    wd = np.array([t.weekday() for t in local])
    hr = np.array([t.hour for t in local])
    weekday = wd < 5
    print(f"{len(ts)} snapshots, {local[0]:%Y-%m-%d} .. {local[-1]:%Y-%m-%d}; evening (18-23h) snapshots {((hr >= 18)).sum()}")

    base, nb = block_index(level, weekday & (hr >= 10) & (hr < 16))
    commute, _ = block_index(level, weekday & (((hr >= 7) & (hr < 10)) | ((hr >= 16) & (hr < 19))))
    evening, ne = block_index(level, hr >= 18)
    E, Cm = evening - base, commute - base

    S = dict(np.load(OUT_DIR / "supply.npz"))
    blocks = S["edge"]
    nblk = level.shape[1]
    res_face = np.isin(S["cls"], [0, 1, 2])
    blk_res = np.zeros(nblk, bool)
    blk_res[blocks[res_face]] = True
    blk_xy = np.zeros((nblk, 2))
    cnt = np.bincount(blocks, minlength=nblk)
    for k in (0, 1):
        blk_xy[:, k] = np.bincount(blocks, weights=S["xy"][:, k], minlength=nblk) / np.maximum(cnt, 1)
    blk_core = np.bincount(blocks, weights=S["core"].astype(float), minlength=nblk) > 0

    ok = np.isfinite(E) & np.isfinite(Cm)
    for name, m in (("arterial", ok & ~blk_res & blk_core), ("residential", ok & blk_res & blk_core)):
        print(f"  {name:11s} blocks {m.sum():5d}: commute index median {np.nanmedian(Cm[m]):+.3f}, evening index median {np.nanmedian(E[m]):+.3f};"
              f" evening-peaked (evening > commute and > 0.05): {np.mean((E[m] > Cm[m]) & (E[m] > 0.05)):.0%}")

    # cells (150 m) over residential blocks in the core
    ck = np.floor(blk_xy / CELL_M).astype(np.int64)
    ck = ck[:, 0] * 1_000_000 + ck[:, 1]
    sel = ok & blk_res & blk_core
    keys = np.unique(ck[sel])
    pos = {k: i for i, k in enumerate(keys)}
    ci = np.array([pos.get(k, -1) for k in ck])
    s2 = sel & (ci >= 0)
    nb_cell = np.bincount(ci[s2], minlength=len(keys))
    E_cell = np.bincount(ci[s2], weights=E[s2], minlength=len(keys)) / np.maximum(nb_cell, 1)
    Ev_cell = np.bincount(ci[s2], weights=evening[s2], minlength=len(keys)) / np.maximum(nb_cell, 1)

    # evidence per cell: curb km, night double parking, evening permit tickets, simulated evening contention
    fk = np.floor(S["xy"] / CELL_M).astype(np.int64)
    fk = fk[:, 0] * 1_000_000 + fk[:, 1]
    fc = np.array([pos.get(k, -1) for k in fk])
    curb = S["core"] & (S["capacity"] >= 0.5) & (fc >= 0)
    agg = lambda w: np.bincount(fc[curb], weights=np.asarray(w, float)[curb], minlength=len(keys))
    km = agg(S["length_m"]) / 1000
    dbl = agg(S["night_double"]) / np.maximum(km, 1e-9)
    pts, own = curb_samples(S)
    P = np.load(CURB_DIR / "citations_ppd_2023plus.npy")
    eve = (P[:, 3] >= 18) | (P[:, 3] < 2)
    d, j = cKDTree(pts).query(xy(P[eve, 0], P[eve, 1]), distance_upper_bound=12)
    ppd_face = np.bincount(own[j[np.isfinite(d)]], minlength=len(S["capacity"])).astype(float)
    ppd = agg(ppd_face) / np.maximum(km, 1e-9)
    Ex = json.load(gzip.open(OUT_DIR / "contention_week.json.gz", "rt"))
    ids = np.array([f["id"] for f in Ex["faces"]])
    pf = np.full(len(S["capacity"]), np.nan)
    pf[ids] = np.mean([np.array(Ex["p_free"][d])[:, 76:96].mean(1) for d in range(5)], 0) / 100  # weekdays 19-24h
    sim = agg(np.nan_to_num(1 - pf) * S["capacity"]) / np.maximum(agg(S["capacity"]), 1e-9)
    good = (nb_cell >= 2) & (km > 0.3)
    print(f"\n{good.sum()} cells with residential traffic coverage (core)")
    for name, v in (("evening slowdown vs own midday", E_cell), ("raw evening level", Ev_cell)):
        print(f"  {name:31s}: vs evening permit tickets {spearmanr(v[good], ppd[good]).statistic:+.3f}, "
              f"vs night double parking {spearmanr(v[good], dbl[good]).statistic:+.3f}, "
              f"vs simulated evening contention {spearmanr(v[good], sim[good]).statistic:+.3f}")
    print(f"  (reference) simulated evening contention: vs evening permit tickets {spearmanr(sim[good], ppd[good]).statistic:+.3f},"
          f" vs night double parking {spearmanr(sim[good], dbl[good]).statistic:+.3f}")
    np.savez(OUT_DIR / "traffic_signal.npz", base=base, commute=commute, evening=evening, block_res=blk_res, block_xy=blk_xy)


if __name__ == "__main__":
    main()
