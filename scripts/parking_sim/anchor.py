"""Midday anchor: expected curbside cars per 150 m cell from the validated statistical model (curb/occupancy_model.py).

The bottom-up demand models get the daily rhythm right but not where cars are at midday (Spearman ~0 against the
2010 aerial), while the statistical model trained on satellite counts reaches ~0.6 out of sample. The simulator
therefore scales each cell's resident stock so its simulated midday count matches this anchor.
"""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "curb"))
from common import BANDS, CELL_M, CLASSES, CURB_DIR, M_LAT, M_LON, curb_cells, features, gpx_to_ll, roads, xy  # noqa: E402
from occupancy_model import SETS, boosted, design  # noqa: E402

RECALL = 0.7  # detector recall on the 2010 aerial; observed counts are scaled up by this


def cell_structure(keys):
    """Curb km (both sides) and road-class shares for arbitrary cells, ignoring imagery coverage."""
    P = roads()
    mx, my = (P[:, 0] + P[:, 2]) / 2, (P[:, 1] + P[:, 3]) / 2
    pc = np.floor(xy(*gpx_to_ll(mx, my)) / CELL_M).astype(np.int64)
    pk = pc[:, 0] * 1_000_000 + pc[:, 1]
    tot = defaultdict(float)
    bycls = defaultdict(lambda: np.zeros(len(CLASSES)))
    for k, l, c in zip(pk, P[:, 6], P[:, 4].astype(int)):
        tot[k] += 2 * l
        bycls[k][c] += 2 * l
    curb_km = np.array([tot.get(k, 0.0) for k in keys]) / 1000
    share = np.array([bycls[k] / tot[k] if tot.get(k) else np.zeros(len(CLASSES)) for k in keys])
    return dict(curb_km=np.maximum(curb_km, 0.05), cls_share=share)


def midday_targets(keys, exclude_bbox=None):
    """Expected curbside cars at midday per cell (both sides), corrected for detector recall.

    exclude_bbox: leave the area's cells out of training, so validation inside it stays out of sample."""
    C = curb_cells(CURB_DIR / "dets_10_ft.npz", BANDS["10"])
    F = features(C["centers"])
    X = design(C, F, SETS[2])  # street + people + buildings (busyness adds nothing at midday)
    y = np.log((C["ncar"] + 1) / C["curb_km"])
    train = np.ones(len(y), bool)
    if exclude_bbox is not None:
        lat, lon = C["centers"][:, 1] / M_LAT, C["centers"][:, 0] / M_LON
        b = exclude_bbox
        train = ~((lat >= b[0]) & (lat <= b[1]) & (lon >= b[2]) & (lon <= b[3]))
    model = boosted().fit(X[train], y[train])
    keys = np.asarray(keys)
    centers = np.c_[(keys // 1_000_000 + 0.5) * CELL_M, (keys % 1_000_000 + 0.5) * CELL_M]
    st = cell_structure(keys)
    Xt = design(st, features(centers), SETS[2])
    density = np.exp(model.predict(Xt)) - 1 / st["curb_km"]
    return np.maximum(density, 0) * st["curb_km"] / RECALL
