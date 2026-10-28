#!/usr/bin/env python3
"""Build the contention map viewer (one self-contained HTML page) from the weekly export.

Usage: python3 scripts/parking_sim/build_viewer.py   (after export.py)
Writes data/parking_sim/viewer.html. Faces are drawn 6 m off the centerline toward their side; values are hourly
means of the 15-minute slots. Validation numbers in the page are static text in viewer_template.html.
"""
import base64
import os
import gzip
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "curb"))
from common import M_LAT, M_LON, xy  # noqa: E402
from supply import OUT_DIR  # noqa: E402

CLASS_NAMES = ["residential", "unclassified", "living street", "tertiary", "secondary", "primary"]


def main():
    S = np.load(OUT_DIR / "supply.npz")
    E = json.load(gzip.open(OUT_DIR / "contention_week.json.gz", "rt"))
    F = E["faces"]
    ids = np.array([f["id"] for f in F])
    n = len(F)
    lat0, lon0 = float(S["bbox"][0]), float(S["bbox"][2])
    lines = []
    for f in F:
        L = np.array(f["line"])
        m = xy(L[:, 0], L[:, 1])
        t = np.gradient(m, axis=0)
        t /= np.maximum(np.hypot(*t.T), 1e-9)[:, None]
        o = m + np.c_[-t[:, 1], t[:, 0]] * (1 if f["side"] == "L" else -1) * 6.0
        la, lo = o[:, 1] / M_LAT, o[:, 0] / M_LON
        lines.append([int(round(v)) for pair in zip((la - lat0) * 1e5, (lo - lon0) * 1e5) for v in pair])
    pf = np.rint(np.array(E["p_free"]).reshape(7, n, 24, 4).mean(3)).astype(np.uint8).transpose(1, 0, 2)
    # rule in effect for a visitor, week of the export: 0 legal, 1 street cleaning, 2 tow-away / no stopping, 3 permit
    week = E["week_of_month"]
    sw = S["sweep_slots"][ids] & S["sweep_weeks"][ids][:, week - 1][:, None, None]
    rule = np.zeros((n, 7, 96), np.uint8)
    rule[S["permit"][ids]] = 3
    rule[S["nopark"][ids]] = 2
    rule[sw] = 1
    R4 = rule.reshape(n, 7, 24, 4)
    rh = np.zeros((n, 7, 24), np.uint8)
    for code in (3, 2, 1):
        rh[(R4 == code).sum(3) >= 2] = code
    cap = np.array([f["capacity"] for f in F], float)
    legal = rh == 0
    hard = (pf < 20) & legal
    share = np.round(100 * (hard * cap[:, None, None]).sum(0) / np.maximum((legal * cap[:, None, None]).sum(0), 1), 1)
    med = [[float(np.median(pf[legal[:, d, h], d, h])) if legal[:, d, h].any() else 0.0 for h in range(24)] for d in range(7)]
    data = dict(origin=[lat0, lon0], lines=lines,
                meta=[[["L", "R"].index(f["side"]), CLASS_NAMES.index(f["cls"].replace("_", " ")), f["capacity"]] for f in F],
                pfree=base64.b64encode(gzip.compress(pf.tobytes(), 9)).decode(),
                rule=base64.b64encode(gzip.compress(rh.tobytes(), 9)).decode(),
                hardShare=share.tolist(), medianFree=med, spaces=int(cap.sum()), faces=n,
                title=os.environ.get("PARKSIM_TITLE"), areaText=os.environ.get("PARKSIM_AREA_TEXT"))
    page = (HERE / "viewer_template.html").read_text().replace("__DATA__", json.dumps(data, separators=(",", ":")))
    dest = OUT_DIR / "viewer.html"
    dest.write_text(page)
    print(f"{n} faces -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
