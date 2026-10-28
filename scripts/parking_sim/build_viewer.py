#!/usr/bin/env python3
"""Build the contention map viewer (one self-contained HTML page) from the weekly export.

Usage: python3 scripts/parking_sim/build_viewer.py   (after export.py)
Writes data/parking_sim/viewer.html. Faces are drawn 6 m off the centerline toward their side; values are hourly
means of the 15-minute slots. Validation numbers in the page are static text in viewer_template.html.
"""
import base64
import os
import gzip
import html
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "curb"))
from common import M_LAT, M_LON, xy  # noqa: E402
from supply import OUT_DIR  # noqa: E402

CLASS_NAMES = ["residential", "unclassified", "living street", "tertiary", "secondary", "primary"]
# Region pages (PARKSIM_VAL_TABLE=region): held-out results per area, ranges across 3 seeds (README, 2026-10-28)
REGION_VALIDATION = dict(
    title="How the simulator checks out (each area held out)",
    columns=["Check (held-out data)", "K-town core", "Mid-City–Fairfax", "Koreatown–Westlake", "South LA"],
    rows=[["Street-cleaning tickets per route area, ρ", "0.84–0.86", "0.85–0.91", "0.54–0.57", "0.79–0.83"],
          ["Cars left at the sweep vs tickets per sweep", "30–37 vs 36", "34–35 vs 32", "29–33 vs 38", "45–52 vs 38"],
          ["Evening permit-street tickets vs outsiders turned away, ρ", "0.50–0.52", "0.43–0.44", "0.72", "0.07–0.08"],
          ["Night double parking vs occupancy, ρ", "0.29–0.33", "0.25–0.26", "0.24–0.26", "0.50–0.53"],
          ["Same, beyond what capacity alone explains", "0.07–0.11", "0.18–0.19", "0.21–0.22", "0.30–0.34"],
          ["Midday cars, 2010 aerial (a Monday, 12–2 PM), ρ", "0.25–0.30", "0.22–0.30", "0.14–0.18", "0.36–0.37"],
          ["Satellite estimator alone, same cells", "0.39", "0.33", "0.41", "0.47"]],
    note="Each area's satellite anchor is trained without that area. In the simulation, the suggested loops get drivers to the door "
         "in 4.7 min on average vs 8.7 min for typical circling (1,200 trips across the region). South LA's permit "
         "streets are not reproduced yet: strip-mall lots are missing from the data.")


def main():
    S = dict(np.load(OUT_DIR / "supply.npz"))
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
                title=os.environ.get("PARKSIM_TITLE"), areaText=os.environ.get("PARKSIM_AREA_TEXT"),
                validation=REGION_VALIDATION if os.environ.get("PARKSIM_VAL_TABLE") == "region" else None)
    page = (HERE / "viewer_template.html").read_text().replace("__DATA__", json.dumps(data, separators=(",", ":")))
    if data["title"]:   # the tab / gallery name, not just the heading
        page = page.replace("<title>K-town Curb Contention</title>", f"<title>{html.escape(data['title'])}</title>", 1)
    dest = OUT_DIR / "viewer.html"
    dest.write_text(page)
    print(f"{n} faces -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
