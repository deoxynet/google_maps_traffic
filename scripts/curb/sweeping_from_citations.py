#!/usr/bin/env python3
"""Check that street-sweeping rules can be read off citations: infer day/weeks/window per posted LA route area.

Usage: python3 scripts/curb/sweeping_from_citations.py   (needs extract_citations.py sweep first)
LA's street_sweeping.db 'odd_even' means weeks of the month (Odd = 1st & 3rd, Even = 2nd & 4th), not street side.
Route pairs like '5P253 Tu' / '5P253 W' share one corridor polygon, one side of the street each day.
2026-10-27 baseline: days 98%, weeks 99%, both 97% of 434 areas; median 98% of citations inside posted windows.
"""
import json
import sqlite3
from collections import defaultdict

import numpy as np
import shapely
from shapely.geometry import shape

from common import CURB_DIR, LA_PARKING_DB


def hm(s):
    return int(s[:2]) + int(s[3:5]) / 60


def main():
    C = np.load(CURB_DIR / "citations_sweep_2023plus.npy")
    db = sqlite3.connect(f"file:{LA_PARKING_DB / 'street_sweeping.db'}?mode=ro", uri=True)
    R = db.execute("select route, start_time, end_time, sweep_days, week_1, week_2, week_3, week_4, is_weekly, geometry "
                   "from routes where city='la' and geometry is not null and start_time is not null").fetchall()
    groups = defaultdict(list)
    for r in R:
        groups[r[0].split()[0]].append(r)
    pts = shapely.points(C[:, 1], C[:, 0])
    res = []
    for rs in groups.values():
        poly = shapely.union_all([shapely.make_valid(shape(json.loads(r[9]))) for r in rs])
        c = C[shapely.contains(poly, pts)]
        if len(c) < 50:
            continue
        posted_days = {d for r in rs for d in json.loads(r[3]) or []}
        posted_weeks = set()
        for r in rs:
            posted_weeks |= {1, 2, 3, 4} if r[8] else {i + 1 for i, f in enumerate(r[4:8]) if f}
        share = np.bincount(c[:, 2].astype(int), minlength=7) / len(c)
        in_win = np.zeros(len(c), bool)
        for r in rs:
            in_win |= (c[:, 3] >= hm(r[1]) - 0.25) & (c[:, 3] < hm(r[2]) + 0.25)
        wom = ((c[:, 4] - 1) // 7 + 1).astype(int)
        weeks = {k for k in (1, 2, 3, 4) if np.mean(wom == k) >= 0.12}
        days = {d for d in range(7) if share[d] >= 0.15}
        res.append((days == posted_days, weeks == posted_weeks, in_win.mean()))
    res = np.array(res, dtype=float)
    print(f"{len(groups)} route areas, {len(res)} with >=50 citations")
    print(f"  days right {res[:, 0].mean():.0%}, weeks right {res[:, 1].mean():.0%}, both {(res[:, 0] * res[:, 1]).mean():.0%};"
          f" median {np.median(res[:, 2]):.0%} of citations inside posted windows")


if __name__ == "__main__":
    main()
