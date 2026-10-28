#!/usr/bin/env python3
"""Pull the scraped Google traffic history (R2 bucket gmaps-traffic, z15 snapshots) onto the simulator's blocks.

Each snapshot is streamed from R2 and never stored. Traffic segments are sampled every ~8 m and assigned to the
nearest OSM block within 8 m whose direction matches (within 30 degrees), so cross streets at intersections don't
leak in. Output: per snapshot x block, mean congestion level (0 normal .. 3 severe) and how many points covered it.

Usage: python3 scripts/parking_sim/traffic_profiles.py [--workers 8] [--limit N]
Credentials: read-only R2 key file (default gmaps-traffic-readonly-api-key.txt in the repo root, git-ignored) with
ACCESS_KEY_ID and SECRET_ACCESS_KEY lines; the Cloudflare account id comes from R2_ACCOUNT_ID or an ACCOUNT_ID line in
that file (`wrangler whoami` shows it). Writes data/parking_sim/traffic_z15.npz
"""
import argparse
import gzip
import json
import math
import os
import re
import subprocess
import sys
from multiprocessing import Pool
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "curb"))
from common import BASE_DIR, M_LAT, M_LON, xy  # noqa: E402
from supply import OUT_DIR  # noqa: E402

BUCKET = "gmaps-traffic"
STEP_M, MATCH_M, MAX_ANGLE = 8.0, 8.0, 30.0


def r2_env(keyfile):
    kv = {}
    for line in open(keyfile):
        m = re.match(r"\s*([A-Z_]+)\s*[:=]\s*(\S+)", line)
        if m:
            kv[m.group(1)] = m.group(2)
    env = {k: v for k, v in os.environ.items() if k != "AWS_PROFILE"}
    env.update(AWS_ACCESS_KEY_ID=kv["ACCESS_KEY_ID"], AWS_SECRET_ACCESS_KEY=kv["SECRET_ACCESS_KEY"],
               AWS_DEFAULT_REGION="auto", AWS_CONFIG_FILE="/dev/null")
    account = os.environ.get("R2_ACCOUNT_ID") or kv.get("ACCOUNT_ID")
    if not account:
        sys.exit("Set R2_ACCOUNT_ID (or add an ACCOUNT_ID line to the key file); `wrangler whoami` shows the id.")
    env["R2_ENDPOINT"] = f"https://{account}.r2.cloudflarestorage.com"
    return env


def list_keys(env, zoom):
    out = subprocess.run(["aws", "s3api", "list-objects-v2", "--bucket", BUCKET, "--prefix", f"z{zoom}/",
                          "--query", "Contents[].Key", "--output", "json", "--endpoint-url", env["R2_ENDPOINT"]],
                         env=env, check=True, capture_output=True, text=True).stdout
    return sorted(json.loads(out))


# ---- worker state (built once per process) ----
W = {}


def init_worker(env, bbox):
    S = dict(np.load(OUT_DIR / "supply.npz"))   # load once; a lazy NpzFile re-decompresses on every index
    left = np.where(S["side"] == "L")[0]           # one face per block carries its centerline
    ends = list(S["poly_start"][1:]) + [len(S["poly"])]
    pts, dirs, owner = [], [], []
    for f in left:
        c = xy(S["poly"][S["poly_start"][f]:ends[f], 0], S["poly"][S["poly_start"][f]:ends[f], 1])
        for a, b in zip(c[:-1], c[1:]):
            L = np.hypot(*(b - a))
            if L < 1e-6:
                continue
            k = max(1, int(L // 5))
            for t in np.linspace(0, 1, k, endpoint=False):
                pts.append(a + (b - a) * t)
                dirs.append(math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi)
                owner.append(S["edge"][f])
    W.update(env=env, bbox=bbox, tree=cKDTree(np.array(pts)), dirs=np.array(dirs), owner=np.array(owner),
             nblocks=int(S["edge"].max()) + 1)


def process(key):
    raw = subprocess.run(["aws", "s3", "cp", f"s3://{BUCKET}/{key}", "-", "--endpoint-url", W["env"]["R2_ENDPOINT"], "--only-show-errors"],
                         env=W["env"], capture_output=True)
    if raw.returncode:
        return key, None
    d = json.loads(gzip.decompress(raw.stdout))
    b = W["bbox"]
    P, DIR, LEV = [], [], []
    for f in d["features"]:
        g = np.asarray(f["geometry"], float)
        if not ((g[:, 0] > b[0]) & (g[:, 0] < b[1]) & (g[:, 1] > b[2]) & (g[:, 1] < b[3])).any():
            continue
        m = xy(g[:, 0], g[:, 1])
        for s in f["segments"]:
            lev = s["congestion"]
            if lev > 3:
                continue
            for i in range(s["start"], min(s["end"], len(m) - 1)):
                a, c = m[i], m[i + 1]
                L = np.hypot(*(c - a))
                if L < 1e-6:
                    continue
                k = max(1, int(L // STEP_M))
                for t in np.linspace(0, 1, k, endpoint=False):
                    P.append(a + (c - a) * t)
                    DIR.append(math.atan2(c[1] - a[1], c[0] - a[0]) % math.pi)
                    LEV.append(lev)
    if not P:
        return key, (d["timestamp"], np.zeros(0, int), np.zeros(0), np.zeros(0))
    P, DIR, LEV = np.array(P), np.array(DIR), np.array(LEV, float)
    dist, j = W["tree"].query(P, distance_upper_bound=MATCH_M)
    ok = np.isfinite(dist)
    ang = np.degrees(np.abs((DIR[ok] - W["dirs"][j[ok]] + np.pi / 2) % np.pi - np.pi / 2))
    keep = np.where(ok)[0][ang < MAX_ANGLE]
    blk = W["owner"][j[keep]]
    n = np.bincount(blk, minlength=W["nblocks"])
    s = np.bincount(blk, weights=LEV[keep], minlength=W["nblocks"])
    idx = np.nonzero(n)[0]
    return key, (d["timestamp"], idx, s[idx], n[idx])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--zoom", type=int, default=15)
    ap.add_argument("--keyfile", default=str(BASE_DIR / "gmaps-traffic-readonly-api-key.txt"))
    args = ap.parse_args()
    env = r2_env(args.keyfile)
    S = np.load(OUT_DIR / "supply.npz")
    lat, lon = S["latlon"][:, 0], S["latlon"][:, 1]
    bbox = (lat.min() - 0.002, lat.max() + 0.002, lon.min() - 0.002, lon.max() + 0.002)
    keys = list_keys(env, args.zoom)
    if args.limit:
        keys = keys[:: max(1, len(keys) // args.limit)][: args.limit]
    nblocks = int(S["edge"].max()) + 1
    level = np.full((len(keys), nblocks), np.nan, np.float32)
    cover = np.zeros((len(keys), nblocks), np.uint16)
    stamps = np.zeros(len(keys), np.int64)
    pos = {k: i for i, k in enumerate(keys)}
    failed = 0
    with Pool(args.workers, initializer=init_worker, initargs=(env, bbox)) as pool:
        for done, (key, res) in enumerate(pool.imap_unordered(process, keys), 1):
            if res is None:
                failed += 1
                continue
            i = pos[key]
            stamps[i], idx, s, n = res
            level[i, idx] = s / n
            cover[i, idx] = np.minimum(n, 65535)
            if done % 50 == 0:
                print(f"  {done}/{len(keys)} snapshots", flush=True)
    dest = OUT_DIR / f"traffic_z{args.zoom}.npz"
    np.savez_compressed(dest, keys=np.array(keys), timestamp=stamps, level=level, cover=cover)
    covered = np.isfinite(level).any(0).sum()
    print(f"{len(keys) - failed} snapshots ({failed} failed) x {nblocks} blocks, {covered} blocks with traffic data -> {dest}")


if __name__ == "__main__":
    main()
