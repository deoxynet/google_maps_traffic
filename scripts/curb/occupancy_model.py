#!/usr/bin/env python3
"""Predict curbside cars per km of curb from residents/jobs/buildings/busyness; validate out of sample.

Usage: python3 scripts/curb/occupancy_model.py
A. trains on the 2010 sim-area cells with K-town/Mid-Wilshire held out, scores the held-out cells
B. checks persistence: same K-town cells in 2010 aerial vs 2025 satellite (needs dets_63116_ft.npz)

2026-10-27 baseline (held-out): rho 0.58, R^2 0.35, 'fullest 25%' precision 60% with street+people+buildings;
busyness adds nothing at midday. 2010 vs 2025 on 62 cells: rho 0.40.
"""
import warnings

import numpy as np
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import RidgeCV
from sklearn.metrics import r2_score

from common import BANDS, CURB_DIR, M_LAT, M_LON, curb_cells, features

warnings.filterwarnings("ignore")
L = np.log1p
SETS = [("street",), ("street", "people"), ("street", "people", "buildings"), ("street", "people", "buildings", "busyness")]
HOLDOUT = dict(lat=(34.045, 34.070), lon=(-118.345, -118.280))  # K-town / Mid-Wilshire


def design(C, F, cols):
    parts = {"street": [C["cls_share"], np.log(C["curb_km"])[:, None]],
             "people": [L(F["residents"])[:, None], L(F["jobs"])[:, None]],
             "buildings": [L(F["bldg_count"])[:, None], L(F["bldg_median_area"])[:, None], L(F["bldg_total_area"])[:, None]],
             "busyness": [L(F["busyness"])[:, None], L(F["place_density"])[:, None]]}
    return np.hstack([p for c in cols for p in parts[c]])


def boosted():
    return HistGradientBoostingRegressor(max_depth=4, learning_rate=0.05, max_iter=300, min_samples_leaf=40, random_state=0)


def fullest_quarter_precision(pred, obs):
    po, pp = obs >= np.quantile(obs, 0.75), pred >= np.quantile(pred, 0.75)
    return (po & pp).sum() / pp.sum()


def main():
    C = curb_cells(CURB_DIR / "dets_10_ft.npz", BANDS["10"])
    F = features(C["centers"])
    y = np.log((C["ncar"] + 1) / C["curb_km"])
    lat, lon = C["centers"][:, 1] / M_LAT, C["centers"][:, 0] / M_LON
    test = (lat >= HOLDOUT["lat"][0]) & (lat <= HOLDOUT["lat"][1]) & (lon >= HOLDOUT["lon"][0]) & (lon <= HOLDOUT["lon"][1])
    print(f"{len(y)} cells: train {(~test).sum()}, held out {test.sum()}")
    print("\nA. held-out area                                   R^2    Spearman  fullest-25% precision (chance 25%)")
    for cols in SETS:
        X = design(C, F, cols)
        for name, mdl in (("linear", RidgeCV(alphas=np.logspace(-2, 3, 12))), ("boosted", boosted())):
            p = mdl.fit(X[~test], y[~test]).predict(X[test])
            print(f"  {name:7s} {'+'.join(cols):34s} {r2_score(y[test], p):+.3f}   {spearmanr(p, y[test]).statistic:+.3f}"
                  f"    {fullest_quarter_precision(p, y[test]):.0%}")
    X = design(C, F, SETS[-1])
    full = boosted().fit(X, y)
    print("\nB. persistence, K-town cells imaged in both 2010 and 2025")
    C25 = curb_cells(CURB_DIR / "dets_63116_ft.npz", BANDS["63116"], min_cov=0.5)
    C10 = curb_cells(CURB_DIR / "dets_10_ft.npz", BANDS["10"], min_cov=0.5)
    common, i25, i10 = np.intersect1d(C25["keys"], C10["keys"], return_indices=True)
    d25, d10 = C25["density"][i25], C10["density"][i10]
    print(f"  {len(common)} cells; median cars/curb-km 2010 {np.median(d10):.0f}, 2025 {np.median(d25):.0f};"
          f" Spearman {spearmanr(d10, d25).statistic:+.3f}")
    sub = {k: C25[k][i25] for k in ("cls_share", "curb_km")}
    p25 = full.predict(design(sub, features(C25["centers"][i25]), SETS[-1]))
    print(f"  2010-trained model -> 2025 cells: Spearman {spearmanr(p25, d25).statistic:+.3f}")


if __name__ == "__main__":
    main()
