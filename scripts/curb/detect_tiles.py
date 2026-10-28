#!/usr/bin/env python3
"""Run the fine-tuned vehicle detector over cached Wayback tiles.

Usage: python3 scripts/curb/detect_tiles.py <release>     # e.g. 10 (2010 aerial) or 63116 (2025 satellite)
Writes data/curb/dets_<release>_ft.npz: dets = (gx, gy, theta, length_px, width_px, conf, cls) in z19 pixels,
tiles = (tx, ty) processed. Keep everything >= conf 0.15; filter later.
"""
import glob
import os
import sys
import time

import numpy as np
from ultralytics import YOLO

from common import CURB_DIR, MODEL_PATH, TILES_DIR


def main():
    release = sys.argv[1]
    tiles = sorted(glob.glob(str(TILES_DIR / release / "*.jpg")))
    model = YOLO(str(MODEL_PATH))
    out, done, t = [], [], time.time()
    for i in range(0, len(tiles), 32):
        batch = tiles[i:i + 32]
        for path, r in zip(batch, model(batch, conf=0.15, imgsz=512, device="mps", verbose=False)):
            ty, tx = map(int, os.path.basename(path)[:-4].split("_"))
            done.append((tx, ty))
            o = r.obb
            for (cx, cy, w, h, a), c, cl in zip(o.xywhr.cpu().numpy(), o.conf.cpu().numpy(), o.cls.cpu().numpy()):
                if int(cl) not in (9, 10):  # DOTA small / large vehicle
                    continue
                L, W, th = (w, h, a) if w >= h else (h, w, a + np.pi / 2)
                out.append((tx * 256 + cx, ty * 256 + cy, th % np.pi, L, W, c, cl))
        if i % 3200 == 0:
            print(f"{i + len(batch)}/{len(tiles)} tiles, {len(out)} vehicles, {time.time() - t:.0f}s", flush=True)
    CURB_DIR.mkdir(parents=True, exist_ok=True)
    dest = CURB_DIR / f"dets_{release}_ft.npz"
    np.savez(dest, dets=np.array(out, np.float64), tiles=np.array(done, np.int64))
    print(f"{len(out)} vehicles on {len(done)} tiles -> {dest}")


if __name__ == "__main__":
    main()
