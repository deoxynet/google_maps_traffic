#!/usr/bin/env python3
"""Fine-tune yolov8n-obb on the verified parking tiles (dataset/) and report recall on the test split.

Usage: DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib python3 scripts/curb/train_detector.py [--epochs 40]
The DYLD path works around Homebrew python3.14's pyexpat/libexpat mismatch (matplotlib import during training).
Launch python directly: nohup and env are SIP-protected and strip DYLD_* variables.

Stock DOTA weights find only 18-32% of labeled cars; this recipe reached 96% recall / 79% precision (conf 0.25)
and 89% / 88% (conf 0.45) on the 97 held-out tiles. Expect lower recall on other imagery (~65-75% on 2010 aerial).
"""
import argparse
import glob
import os
import shutil

import numpy as np
from scipy.spatial import cKDTree

from common import BASE_DIR, MODEL_PATH


def recall_precision(weights, imgsz, thresholds=(0.15, 0.25, 0.45)):
    from ultralytics import YOLO
    imgs = sorted(glob.glob(str(BASE_DIR / "dataset" / "images" / "test" / "*.jpg")))
    gt = {}
    for p in imgs:
        lp = p.replace("/images/", "/labels/").rsplit(".", 1)[0] + ".txt"
        pts = []
        if os.path.exists(lp):
            for line in open(lp):
                v = line.split()
                if v and int(v[0]) in (9, 10):
                    pts.append(np.array(v[1:9], float).reshape(4, 2).mean(0) * 256)
        gt[p] = np.array(pts).reshape(-1, 2)
    m = YOLO(str(weights))
    preds = {}
    for i in range(0, len(imgs), 16):
        for p, r in zip(imgs[i:i + 16], m(imgs[i:i + 16], conf=0.05, imgsz=imgsz, device="mps", verbose=False)):
            keep = np.isin(r.obb.cls.cpu().numpy(), [9, 10])
            preds[p] = (r.obb.xywhr.cpu().numpy()[keep][:, :2], r.obb.conf.cpu().numpy()[keep])
    for th in thresholds:
        tp = fp = fn = 0
        for p in imgs:
            c, s = preds[p]
            c, g = c[s >= th], gt[p]
            if len(g) == 0:
                fp += len(c)
                continue
            if len(c) == 0:
                fn += len(g)
                continue
            d, j = cKDTree(c).query(g)
            hit = d < 10  # within ~2.5 m
            tp += hit.sum()
            fn += len(g) - hit.sum()
            fp += len(c) - len(set(j[hit]))
        print(f"  conf>={th}: recall {tp / (tp + fn):.0%}, precision {tp / max(tp + fp, 1):.0%}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--imgsz", type=int, default=512)
    ap.add_argument("--eval-only", action="store_true")
    args = ap.parse_args()
    if not args.eval_only:
        from ultralytics import YOLO
        m = YOLO(str(BASE_DIR / "yolov8n-obb.pt"))
        m.train(data=str(BASE_DIR / "dataset" / "data.yaml"), epochs=args.epochs, imgsz=args.imgsz, batch=16,
                device="mps", lr0=0.002, degrees=180, flipud=0.5, mosaic=0.0, patience=15,
                project=str(BASE_DIR / "runs" / "curb"), name="ft", exist_ok=True, plots=False, verbose=False)
        MODEL_PATH.parent.mkdir(exist_ok=True)
        shutil.copy(BASE_DIR / "runs" / "curb" / "ft" / "weights" / "best.pt", MODEL_PATH)
    print(f"{MODEL_PATH} on dataset/images/test:")
    recall_precision(MODEL_PATH, args.imgsz)


if __name__ == "__main__":
    main()
