#!/usr/bin/env python3
"""Print lateral-offset histograms of detected cars vs OSM centerlines, per road class.

Usage: python3 scripts/curb/calibrate_offsets.py <release>
Parallel cars bunch in the curb lane (residential: 3-6 m on 2010 aerial); driveway cars sit 10-14 m out.
Use the peaks to set BANDS[<release>] in common.py.
"""
import sys

import numpy as np

from common import CLASSES, CURB_DIR, roads, snap


def main():
    release = sys.argv[1]
    D = np.load(CURB_DIR / f"dets_{release}_ft.npz")["dets"]
    D = D[D[:, 5] >= 0.15]
    lat, ang, cls, _ = snap(D, roads())
    edges = np.arange(0, 16, 1.0)
    for c, name in enumerate(CLASSES):
        m = cls == c
        if m.sum() < 200:
            continue
        hp, _ = np.histogram(lat[m & (ang < 15)], edges)
        hq, _ = np.histogram(lat[m & (ang > 75)], edges)
        print(f"\n{name}: {m.sum()} cars. lateral offset histogram, 1 m bins 0..15")
        print("  parallel     ", " ".join(f"{v:6d}" for v in hp))
        print("  perpendicular", " ".join(f"{v:6d}" for v in hq))


if __name__ == "__main__":
    main()
