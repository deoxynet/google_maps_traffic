"""Shared helpers for curb-parking analysis: tile math, OSM road snapping, per-cell curb counts, features.

Coordinates: detections are stored in global Web Mercator pixels at z19 (gx, gy).
Analysis grids use a local equirectangular metre frame (see `xy`).
"""
import csv
import gzip
import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
CURB_DIR = DATA_DIR / "curb"
TILES_DIR = DATA_DIR / "tiles" / "z19"
MODEL_PATH = BASE_DIR / "models" / "curb_obb_ft512.pt"
ROADS_DB = DATA_DIR / "osm_roads.db"
BUILDINGS_DB = DATA_DIR / "osm_buildings.db"
POPULAR_TIMES_DB = DATA_DIR / "popular_times" / "collector.db"
LODES_DIR = DATA_DIR / "lodes"
LA_PARKING_DB = Path.home() / "git" / "la-parking" / "db"

Z = 19
N = 2 ** Z
WORLD = N * 256
MPP = 156543.03392 * math.cos(math.radians(34.015)) / N  # metres per z19 pixel in central LA
M_LAT = 111320.0
M_LON = 111320.0 * math.cos(math.radians(34.015))
CELL_M = 150.0

CLASSES = ("residential", "unclassified", "living_street", "tertiary", "secondary", "primary")
RESIDENTIAL_CLASSES = ("residential", "unclassified", "living_street")

# Curb-lane band (metres from OSM centerline) per road class, read off the lateral-offset
# histograms of parallel cars (calibrate_offsets.py). Recalibrate per imagery source:
# off-nadir 2025 satellite tiles sit ~1.5 m off OSM, splitting the residential peak in two.
BANDS = {
    "10": {"residential": [2.5, 6.5], "unclassified": [3, 7], "living_street": [2.5, 6.5],
           "tertiary": [4, 8.5], "secondary": [6, 10], "primary": [7, 11]},
    "63116": {"residential": [2.5, 8.5], "unclassified": [2.5, 8.5], "living_street": [2.5, 8.5],
              "tertiary": [3.5, 10], "secondary": [5.5, 11.5], "primary": [6.5, 12.5]},
}


def xy(lat, lon):
    return np.c_[np.asarray(lon) * M_LON, np.asarray(lat) * M_LAT]


def ll_to_gpx(lat, lon):
    lat = np.radians(np.asarray(lat, float))
    x = (np.asarray(lon, float) + 180) / 360 * WORLD
    y = (1 - np.log(np.tan(lat) + 1 / np.cos(lat)) / math.pi) / 2 * WORLD
    return x, y


def gpx_to_ll(x, y):
    lon = np.asarray(x) / WORLD * 360 - 180
    lat = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * np.asarray(y) / WORLD))))
    return lat, lon


def load_roads(bbox=(33.95, 34.08, -118.41, -118.24), piece_m=15.0):
    """OSM street segments split into <=15 m pieces: (ax, ay, bx, by, class, lanes, length_m) in z19 pixels."""
    db = sqlite3.connect(f"file:{ROADS_DB}?mode=ro", uri=True)
    q = (f"select start_lat,start_lon,end_lat,end_lon,lanes,highway from road_segments "
         f"where highway in ({','.join('?' * len(CLASSES))}) and start_lat between ? and ? and start_lon between ? and ?")
    R = db.execute(q, (*CLASSES, bbox[0], bbox[1], bbox[2], bbox[3])).fetchall()
    ax, ay = ll_to_gpx([r[0] for r in R], [r[1] for r in R])
    bx, by = ll_to_gpx([r[2] for r in R], [r[3] for r in R])
    pieces = []
    for i, r in enumerate(R):
        L = math.hypot(bx[i] - ax[i], by[i] - ay[i]) * MPP
        k = max(1, int(math.ceil(L / piece_m)))
        for j in range(k):
            t0, t1 = j / k, (j + 1) / k
            pieces.append((ax[i] + (bx[i] - ax[i]) * t0, ay[i] + (by[i] - ay[i]) * t0,
                           ax[i] + (bx[i] - ax[i]) * t1, ay[i] + (by[i] - ay[i]) * t1,
                           CLASSES.index(r[5]), r[4], L / k))
    return np.array(pieces)


def snap(dets, P, k=10):
    """Per detection (gx, gy, theta): lateral distance to nearest street (m), misalignment (deg), class, piece index."""
    mid = np.c_[(P[:, 0] + P[:, 2]) / 2, (P[:, 1] + P[:, 3]) / 2]
    _, nb = cKDTree(mid).query(dets[:, :2], k=k)
    A = P[nb][:, :, 0:2]
    B = P[nb][:, :, 2:4]
    p = dets[:, None, :2]
    d = B - A
    t = np.clip(((p - A) * d).sum(-1) / np.maximum((d ** 2).sum(-1), 1e-9), 0, 1)
    dist = np.hypot(*(p - (A + t[..., None] * d)).transpose(2, 0, 1)) * MPP
    j = dist.argmin(1)
    rows = np.arange(len(dets))
    idx = nb[rows, j]
    road_th = np.arctan2(P[idx, 3] - P[idx, 1], P[idx, 2] - P[idx, 0]) % np.pi
    ang = np.degrees(np.abs((dets[:, 2] - road_th + np.pi / 2) % np.pi - np.pi / 2))
    return dist[rows, j], ang, P[idx, 4].astype(int), idx


_ROADS = None


def roads():
    global _ROADS
    if _ROADS is None:
        _ROADS = load_roads()
    return _ROADS


def curb_cells(dets_file, bands, conf=0.15, max_misalign=20.0, min_cov=0.95):
    """Curbside-parallel cars per 150 m cell, counting only curb whose tile was imaged."""
    z = np.load(dets_file)
    D = z["dets"]
    D = D[D[:, 5] >= conf]
    tiles = set(map(tuple, z["tiles"]))
    P = roads()
    lat_off, ang, cls, _ = snap(D, P)
    band = np.array([bands.get(c, [99, 99]) for c in CLASSES])
    curb = (ang < max_misalign) & (lat_off >= band[cls, 0]) & (lat_off <= band[cls, 1])
    mx, my = (P[:, 0] + P[:, 2]) / 2, (P[:, 1] + P[:, 3]) / 2
    covered = np.array([(int(a // 256), int(b // 256)) in tiles for a, b in zip(mx, my)])
    pc = np.floor(xy(*gpx_to_ll(mx, my)) / CELL_M).astype(np.int64)
    dc = np.floor(xy(*gpx_to_ll(D[curb, 0], D[curb, 1])) / CELL_M).astype(np.int64)
    key = lambda a: a[:, 0] * 1_000_000 + a[:, 1]
    tot, cov, cars = defaultdict(float), defaultdict(float), defaultdict(int)
    bycls = defaultdict(lambda: np.zeros(len(CLASSES)))
    for k, l, c, cv in zip(key(pc), P[:, 6], P[:, 4].astype(int), covered):
        tot[k] += 2 * l  # both sides of the street
        if cv:
            cov[k] += 2 * l
            bycls[k][c] += 2 * l
    for k in key(dc):
        cars[k] += 1
    keys = np.array(sorted(k for k in tot if cov[k] / tot[k] >= min_cov and cov[k] >= 300))
    out = dict(keys=keys,
               centers=np.c_[(keys // 1_000_000 + 0.5) * CELL_M, (keys % 1_000_000 + 0.5) * CELL_M],
               curb_km=np.array([cov[k] for k in keys]) / 1000,
               ncar=np.array([cars.get(k, 0) for k in keys]),
               cls_share=np.array([bycls[k] / cov[k] for k in keys]))
    out["density"] = out["ncar"] / out["curb_km"]
    return out


def lodes_blocks():
    """(block lat/lon, workers living there, jobs located there) for LA County blocks, LODES 2023."""
    blk = {}
    with open(LODES_DIR / "la_blocks.csv") as f:
        for r in csv.DictReader(f):
            blk[r["block_fips"]] = (float(r["lat"]), float(r["lon"]))
    home, work = defaultdict(int), defaultdict(int)
    with gzip.open(LODES_DIR / "ca_od_main_JT00_2023.csv.gz", "rt") as f:
        next(f)
        for line in f:
            w, h, s = line.split(",", 3)[:3]
            if h.startswith("06037"):
                home[h] += int(s)
            if w.startswith("06037"):
                work[w] += int(s)
    return blk, home, work


def place_profiles():
    """All discovered places with a 7x24 busyness curve (own histogram, else category mean, else global mean)."""
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    pt = sqlite3.connect(f"file:{POPULAR_TIMES_DB}?mode=ro", uri=True)
    rows = pt.execute("select p.lat,p.lng,p.reviews,p.categories,h.status,h.popular_times,coalesce(h.dwell,p.dwell) "
                      "from places p left join histograms h using(fid) where p.lat is not null").fetchall()

    def cat0(c):
        try:
            return (json.loads(c) or [None])[0]
        except Exception:
            return None

    meas, bycat = {}, defaultdict(list)
    for i, r in enumerate(rows):
        if r[4] == "ok":
            a = np.array([json.loads(r[5]).get(k) or [0] * 24 for k in days], float)
            if a.max() > 0:
                meas[i] = a / a.max()
                bycat[cat0(r[3])].append(meas[i])
    gm = np.mean(list(meas.values()), 0)
    cm = {k: np.mean(v, 0) for k, v in bycat.items() if len(v) >= 5}
    prof = np.array([meas.get(i, cm.get(cat0(r[3]), gm)) for i, r in enumerate(rows)])

    def dwell_min(d):
        try:
            v = json.loads(d) if d else None
            return float(np.mean(v)) if v else np.nan
        except Exception:
            return np.nan

    return dict(lat=np.array([r[0] for r in rows]), lon=np.array([r[1] for r in rows]),
                reviews=np.array([r[2] or 0 for r in rows], float), category=[cat0(r[3]) for r in rows],
                profile=prof, measured=np.array([i in meas for i in range(len(rows))]),
                dwell_min=np.array([dwell_min(r[6]) for r in rows]))


def kernel_sum(points_xy, values, centers, R=300.0):
    t = cKDTree(points_xy)
    out = np.zeros(len(centers))
    for i, idx in enumerate(t.query_ball_point(centers, R)):
        if idx:
            out[i] = (values[idx] * np.exp(-np.hypot(*(points_xy[idx] - centers[i]).T) / (R / 2))).sum()
    return out


def features(centers):
    """Per-cell predictors: residents, jobs (LODES), weekday-midday busyness, place density, building stats."""
    F = {}
    blk, home, work = lodes_blocks()
    for name, tbl in (("residents", home), ("jobs", work)):
        ks = [k for k in tbl if k in blk]
        F[name] = kernel_sum(xy([blk[k][0] for k in ks], [blk[k][1] for k in ks]),
                             np.array([tbl[k] for k in ks], float), centers)
    pl = place_profiles()
    pxy = xy(pl["lat"], pl["lon"])
    w = np.sqrt(pl["reviews"])
    F["busyness"] = kernel_sum(pxy, w * pl["profile"][:, 0:5, 10:15].mean((1, 2)), centers, R=400)
    F["place_density"] = kernel_sum(pxy, w, centers, R=400)
    bdb = sqlite3.connect(f"file:{BUILDINGS_DB}?mode=ro", uri=True)
    lat, lon = centers[:, 1] / M_LAT, centers[:, 0] / M_LON
    B = np.array(bdb.execute(
        "select min_lat,max_lat,min_lon,max_lon from buildings_rtree where max_lat>=? and min_lat<=? and max_lon>=? and min_lon<=?",
        (lat.min() - 0.003, lat.max() + 0.003, lon.min() - 0.003, lon.max() + 0.003)).fetchall())
    bc = xy((B[:, 0] + B[:, 1]) / 2, (B[:, 2] + B[:, 3]) / 2)
    area = (B[:, 1] - B[:, 0]) * M_LAT * (B[:, 3] - B[:, 2]) * M_LON
    n, med, tot = (np.zeros(len(centers)) for _ in range(3))
    for i, idx in enumerate(cKDTree(bc).query_ball_point(centers, CELL_M * 0.75)):
        if idx:
            n[i], med[i], tot[i] = len(idx), np.median(area[idx]), area[idx].sum()
    F["bldg_count"], F["bldg_median_area"], F["bldg_total_area"] = n, med, tot
    return F
