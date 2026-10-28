#!/usr/bin/env python3
"""Build the Curb Finder app page: the search optimizer running in the browser, with its data embedded.

Packs the directed street graph (network.npz), each face's hourly chance of getting a space (contention_week.json.gz:
capture rate where measured, else share of time free; scaled by PARKSIM_P_SCALE like optimize.py), and public lots
(lots.json) into app_template.html. Writes data/parking_sim/app.html.
Usage: python3 scripts/parking_sim/build_app.py
"""
import base64
import gzip
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "curb"))
from common import M_LAT, M_LON  # noqa: E402
from supply import OUT_DIR  # noqa: E402

P_SCALE = float(os.environ.get("PARKSIM_P_SCALE", 0.25))
b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def main():
    S = np.load(OUT_DIR / "supply.npz")
    N = np.load(OUT_DIR / "network.npz")
    E = json.load(gzip.open(OUT_DIR / "contention_week.json.gz", "rt"))
    lots = json.load(open(OUT_DIR / "lots.json"))
    lat0, lon0 = float(S["bbox"][0]) - 0.01, float(S["bbox"][2]) - 0.01
    to_m = lambda la, lo: ((np.asarray(lo) - lon0) * M_LON, (np.asarray(la) - lat0) * M_LAT)
    # blocks: one centerline per block (taken from its forward edge, else reversed backward edge)
    ls = N["line_start"]
    le = list(ls[1:]) + [len(N["line"])]
    blocks = {}
    for e in range(len(N["src"])):
        b = int(N["block"][e])
        line = N["line"][ls[e]:le[e]]
        if b not in blocks or N["forward"][e]:
            blocks[b] = line if N["forward"][e] else line[::-1]
    bids = sorted(blocks)
    bpos = {b: i for i, b in enumerate(bids)}
    block_lines = []
    for b in bids:
        x, y = to_m(blocks[b][:, 0], blocks[b][:, 1])
        block_lines.append([int(round(v)) for xy_ in zip(x, y) for v in xy_])   # metres
    nx, ny = to_m(N["node_ll"][:, 0], N["node_ll"][:, 1])
    fx, fy = (S["xy"][:, 0] / M_LON * M_LON - lon0 * M_LON), (S["xy"][:, 1] / M_LAT * M_LAT - lat0 * M_LAT)
    # face chance per day x hour (percent; 0 where no curb parking or no data)
    nf = len(S["xy"])
    pf = np.zeros((nf, 7, 24), np.float32)
    ids = np.array([f["id"] for f in E["faces"]])
    P = np.array(E["p_free"], np.float32).transpose(1, 0, 2)
    if "capture" in E:
        C = np.array(E["capture"], np.float32).transpose(1, 0, 2)
        P = np.where(C >= 0, C, P)
    pf[ids] = P.reshape(len(ids), 7, 24, 4).mean(3)
    pf = np.clip(np.rint(pf * P_SCALE), 0, 100).astype(np.uint8)
    tt = np.clip(np.rint(N["travel_s"]), 1, 255).astype(np.uint8)          # [edge, weekend, hour]
    # legal stay per face, day, hour in quarter hours (0 = no limit): meter max stay or assumed posted limit
    lim = np.clip(S["maxstay"][:, :, ::4] // 15, 0, 255).astype(np.uint8)
    data = dict(
        origin=[lat0, lon0], m_lat=M_LAT, m_lon=M_LON,
        nodes=b64(np.c_[nx, ny].astype(np.float32)),
        edges=dict(src=b64(N["src"].astype(np.uint16)), dst=b64(N["dst"].astype(np.uint16)),
                   block=b64(np.array([bpos[int(b)] for b in N["block"]], np.uint16)),
                   fwd=b64(N["forward"].astype(np.uint8)),
                   face_r=b64(N["face_right"].astype(np.int16)), face_l=b64(N["face_left"].astype(np.int16)),
                   tt=b64(tt), name=[str(x) for x in N["name"]]),
        blocks=block_lines,
        faces=dict(xy=b64(np.c_[fx, fy].astype(np.float32)), p=b64(pf), cap=b64(np.rint(S["capacity"]).astype(np.uint8)),
                   lim=b64(lim)),
        lots=[dict(name=l["name"], op=l["operator"], x=float(to_m(l["lat"], l["lon"])[0]), y=float(to_m(l["lat"], l["lon"])[1]),
                   rate=l["rate_per_hr"], max=l["daily_max"], hours=l["hours"], open=l["open"], verified=l.get("verified", True))
              for l in lots],
        p_scale=P_SCALE, bbox=[float(v) for v in S["bbox"]])
    tpl = (HERE / "app_template.html").read_text()
    page = tpl.replace("__DATA__", json.dumps(data, separators=(",", ":")))
    dest = OUT_DIR / "app.html"
    dest.write_text(page)
    print(f"{len(N['src'])} edges, {len(bids)} blocks, {nf} faces, {len(lots)} lots -> {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
