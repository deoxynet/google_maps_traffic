#!/usr/bin/env python3
"""Validate simulated curb occupancy against independent observations.

  1. satellite midday: curbside cars per km of curb per 150 m cell (2010 aerial) vs simulated 11-13h density.
  2. sweeping: citations per sweep event per posted route area (2023+) vs simulated cars left on swept faces.
  3. night: double-parking tickets 21:00-06:00 per km of curb per cell vs simulated night occupancy (00-05h).
     Double parking happens where legal spaces run out, and those tickets play no part in capacity.
The satellite anchor is trained with the core left out (default), so all three stay out of sample.
In a region, PARKSIM_HOLDOUT="lat0,lat1,lon0,lon1" scores that box instead, with the anchor trained on the rest.
PARKSIM_PAR caps how many days simulate at once (default all 12; big regions need ~1.3 GB each).
PARKSIM_REUSE=1 scores the day files already in PARKSIM_OUT instead of simulating again.

Usage: PARKSIM_ANCHOR=night|midday|none python3 scripts/parking_sim/validate.py
"""
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely
from scipy.stats import rankdata, spearmanr
from shapely.geometry import shape

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "curb"))
from anchor import midday_targets  # noqa: E402
from common import BANDS, CELL_M, CURB_DIR, LA_PARKING_DB, curb_cells  # noqa: E402
from scipy.spatial import cKDTree  # noqa: E402
from common import xy  # noqa: E402
from sim import HOLDOUT  # noqa: E402
from supply import OUT_DIR, curb_samples  # noqa: E402


def run_all(days):
    """Simulate the days in parallel (the anchor is computed once first, then cached)."""
    out = Path(os.environ.get("PARKSIM_OUT", OUT_DIR))
    out.mkdir(parents=True, exist_ok=True)
    if os.environ.get("PARKSIM_REUSE") and all((out / f"contention_wd{wd}_w{wk}.npz").exists() for wd, wk in days):
        return {(wd, wk): dict(np.load(out / f"contention_wd{wd}_w{wk}.npz")) for wd, wk in days}
    cmd = lambda wd, wk: [sys.executable, str(HERE / "sim.py"), "--weekday", str(wd), "--week", str(wk)]
    subprocess.run(cmd(*days[0]), check=True, stdout=subprocess.DEVNULL)
    par = int(os.environ.get("PARKSIM_PAR", len(days)))
    rest = list(days[1:])
    while rest:
        procs = [subprocess.Popen(cmd(wd, wk), stdout=subprocess.DEVNULL) for wd, wk in rest[:par]]
        rest = rest[par:]
        for p in procs:
            if p.wait():
                raise RuntimeError("simulation failed")
    return {(wd, wk): dict(np.load(out / f"contention_wd{wd}_w{wk}.npz")) for wd, wk in days}


def partial_rho(x, y, z):
    """Spearman of x and y with z's (rank) linear effect removed from both: does x add anything beyond z?"""
    r = [rankdata(np.asarray(v, float)) for v in (x, y, z)]   # average ranks: most cells tie at zero tickets
    Z = np.c_[np.ones(len(r[2])), r[2]]
    res = [v - Z @ np.linalg.lstsq(Z, v, rcond=None)[0] for v in r[:2]]
    return float(np.corrcoef(*res)[0, 1])


def cells_of(S):
    k = np.floor(S["xy"] / CELL_M).astype(np.int64)
    return k[:, 0] * 1_000_000 + k[:, 1]


def main():
    S = dict(np.load(OUT_DIR / "supply.npz"))
    box = HOLDOUT or tuple(S["bbox"])
    core = S["core"] if HOLDOUT is None else ((S["latlon"][:, 0] >= box[0]) & (S["latlon"][:, 0] <= box[1]) &
                                              (S["latlon"][:, 1] >= box[2]) & (S["latlon"][:, 1] <= box[3]))
    runs = run_all([(2, 2)] + [(wd, wk) for wd in range(5) for wk in (1, 2) if (wd, wk) != (2, 2)] + [(5, 1)])
    fk = cells_of(S)
    curb = (S["capacity"] >= 0.5) & core
    keys = np.unique(fk[curb])
    pos = {k: i for i, k in enumerate(keys)}
    fc = np.array([pos.get(k, -1) for k in fk])
    sel = curb & (fc >= 0)
    agg = lambda w: np.bincount(fc[sel], weights=np.asarray(w, float)[sel], minlength=len(keys))
    curb_km = agg(S["length_m"]) / 1000          # faces are single sides, so this is both sides
    cap = agg(S["capacity"])
    cfg = {k: v for k, v in os.environ.items() if k.startswith("PARKSIM_") and k != "PARKSIM_OUT"}
    print(f"config {cfg or 'defaults'}; {len(keys)} cells in the {'holdout ' + str(box) if HOLDOUT else 'core'}")

    print("1. satellite midday (2010 aerial, a Monday about 12-14h)")
    C = curb_cells(CURB_DIR / "dets_10_ft.npz", BANDS["10"])
    cpos = {k: i for i, k in enumerate(C["keys"])}
    both = np.array([k in cpos for k in keys]) & (curb_km > 0.2)
    obs = np.array([C["density"][cpos[k]] if k in cpos else np.nan for k in keys])
    T = midday_targets(keys, exclude_bbox=box)
    print(f"  {both.sum()} cells; estimator alone (trained outside): Spearman {spearmanr(T[both] / curb_km[both], obs[both]).statistic:+.3f}")
    # the aerial is a Monday around 12-14h: blocks swept Monday 12-14h show half the cars on the swept side
    # (Tuesday's sides, the mirror image, twice as many); its week of the month is unknown, so both Mondays count
    for name, keys_, sl in (("Monday", [(0, 1), (0, 2)], slice(48, 56)), ("Wednesday", [(2, 2)], slice(44, 52)),
                            ("Saturday", [(5, 1)], slice(44, 52))):
        dens = agg(np.mean([runs[k]["occ"][:, sl].mean(1) for k in keys_], 0)) / np.maximum(curb_km, 1e-9)
        hrs = f"{sl.start // 4}-{sl.stop // 4}h"
        print(f"  simulation {name:9s} {hrs}: Spearman {spearmanr(dens[both], obs[both]).statistic:+.3f};"
              f" median {np.median(dens[both]):.0f} vs observed {np.median(obs[both]):.0f} (/0.7 recall = {np.median(obs[both]) / 0.7:.0f})")

    print("2. street sweeping: simulated cars left on swept faces vs citations per sweep event, by route area")
    cited = sum(r["cited"] for k, r in runs.items() if k[0] < 5)
    area_of = np.array([r.split()[0] if r else "" for r in S["sweep_route"]])
    sim_area, faces_in = defaultdict(float), defaultdict(int)
    for a, c_, cr in zip(area_of, cited, core):
        if a and cr:
            sim_area[a] += c_
            faces_in[a] += 1
    Cit = np.load(CURB_DIR / "citations_sweep_2023plus.npy")
    b = box
    Cit = Cit[(Cit[:, 0] >= b[0]) & (Cit[:, 0] <= b[1]) & (Cit[:, 1] >= b[2]) & (Cit[:, 1] <= b[3])]
    pts = shapely.points(Cit[:, 1], Cit[:, 0])
    db = sqlite3.connect(f"file:{LA_PARKING_DB / 'street_sweeping.db'}?mode=ro", uri=True)
    polys = defaultdict(list)
    for route, geom in db.execute("select route, geometry from routes where city='la' and geometry is not null"):
        polys[route.split()[0]].append(shapely.make_valid(shape(json.loads(geom))))
    xs, ys = [], []
    for a, n in faces_in.items():
        if n >= 10:
            xs.append(sim_area[a])
            ys.append(shapely.contains(shapely.union_all(polys[a]), pts).sum() / (46 * 2))
    xs, ys = np.array(xs), np.array(ys)
    print(f"  {len(xs)} route areas; citations per event median {np.median(ys):.1f}, simulated cars left median {np.median(xs):.1f};"
          f" Spearman {spearmanr(xs, ys).statistic:+.3f}")

    print("3. night: double-parking tickets 21-06h per curb-km vs simulated night occupancy (00-05h), by cell")
    ill = agg(S["night_double"]) / np.maximum(curb_km, 1e-9)
    ok = curb_km > 0.3
    print(f"  {int(agg(S['night_double']).sum())} night double-parking tickets in {ok.sum()} cells; {(ill[ok] > 0).mean():.0%} of cells have any")
    for name, key in (("Wednesday", (2, 2)), ("Saturday", (5, 1))):
        r = runs[key]
        occ_share = agg(r["occ"][:, 0:20].mean(1)) / np.maximum(cap, 1e-9)
        # search effort for drivers arriving 21:00-02:00 with a destination in the cell
        L = r["search_log"]
        L = L[(L[:, 1] >= 84) | (L[:, 1] < 8)]
        dk = np.floor(L[:, 5:7] / CELL_M).astype(np.int64)
        di = np.array([pos.get(k, -1) for k in dk[:, 0] * 1_000_000 + dk[:, 1]])
        g = di >= 0
        n = np.bincount(di[g], minlength=len(keys))
        looked = np.bincount(di[g], weights=L[g, 3], minlength=len(keys)) / np.maximum(n, 1)
        fail = np.bincount(di[g], weights=1 - L[g, 2], minlength=len(keys)) / np.maximum(n, 1)
        k2 = ok & (n >= 5)
        cpk = cap / np.maximum(curb_km, 1e-9)
        print(f"  simulation {name:9s}: occupancy Spearman {spearmanr(occ_share[ok], ill[ok]).statistic:+.3f};"
              f" faces checked per search {spearmanr(looked[k2], ill[k2]).statistic:+.3f}, failed searches"
              f" {spearmanr(fail[k2], ill[k2]).statistic:+.3f} ({k2.sum()} cells); median night occupancy {np.median(occ_share[ok]):.0%}")
        print(f"    beyond capacity per curb-km (partial): occupancy {partial_rho(occ_share[ok], ill[ok], cpk[ok]):+.3f},"
              f" faces checked {partial_rho(looked[k2], ill[k2], cpk[k2]):+.3f}")
    print(f"  benchmarks: satellite estimator per curb-km {spearmanr(T[ok] / curb_km[ok], ill[ok]).statistic:+.3f};"
          f" capacity per curb-km {spearmanr(cap[ok] / curb_km[ok], ill[ok]).statistic:+.3f}")

    print("4. permit districts: preferential-parking tickets per face vs simulated non-residents turned away by permit rules")
    pts, own = curb_samples(S)
    P = np.load(CURB_DIR / "citations_ppd_2023plus.npy")
    d, j = cKDTree(pts).query(xy(P[:, 0], P[:, 1]), distance_upper_bound=12)
    hit = np.isfinite(d)
    tick = np.bincount(own[j[hit]], minlength=len(core)).astype(float)
    eve = hit & ((P[:, 3] >= 18) | (P[:, 3] < 2))
    tick_eve = np.bincount(own[j[eve]], minlength=len(core)).astype(float)
    blocked = sum(r["permit_blocked"].sum(1) for r in runs.values())
    blocked_eve = sum(r["permit_blocked"][:, list(range(72, 96)) + list(range(0, 8))].sum(1) for r in runs.values())
    pf = core & S["permit"].any((1, 2)) & (S["capacity"] >= 0.5)
    print(f"  {pf.sum()} permit faces; all hours: Spearman {spearmanr(tick[pf], blocked[pf]).statistic:+.3f};"
          f" evenings 18-02h: Spearman {spearmanr(tick_eve[pf], blocked_eve[pf]).statistic:+.3f}"
          f" ({int(tick_eve[pf].sum())} tickets); benchmark capacity {spearmanr(tick[pf], S['capacity'][pf]).statistic:+.3f}")
    print(f"    beyond capacity (partial): all hours {partial_rho(blocked[pf], tick[pf], S['capacity'][pf]):+.3f},"
          f" evenings {partial_rho(blocked_eve[pf], tick_eve[pf], S['capacity'][pf]):+.3f}")
    vis = sum((r["search_log"][:, 0] == 2).sum() for k, r in runs.items() if k == (2, 2))
    print(f"  visitor curb searches on a simulated Wednesday: {vis:,}")


if __name__ == "__main__":
    main()
