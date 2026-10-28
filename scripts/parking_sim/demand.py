#!/usr/bin/env python3
"""Demand sources/sinks for the parking simulator: residents, inbound commuters, visitors.

Every source becomes "cars wanting a curb space near point P from time a to time b". Parameters marked CAL were
set on 2026-10-27 against satellite midday counts, sweeping, night double parking and evening permit-street tickets
(validate.py): visitor scale 0.25 by day and x3 from 17:00-02:00, commuter curb share 0.05, share of street-parked
resident cars that commute 0.5. Override with PARKSIM_* env vars.

  residents  parcels (units by use type, off-street spaces) scaled to LODES workers-by-home; commute + errands
  commuters  LODES jobs by work block in the area
  visitors   places' busyness / dwell -> arrivals; places beside a lot (or big-box categories) mostly park off-street
"""
import csv
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "curb"))
from common import BASE_DIR, BUILDINGS_DB, LA_PARKING_DB, LODES_DIR, M_LAT, M_LON, lodes_blocks, place_profiles, xy  # noqa: E402

# --- residents (CAL) ---
UNITS = {"Single": 1, "Two Units": 2, "Three Units (Any Combination)": 3, "Four Units (Any Combination)": 4}
VEH_PER_UNIT = {"Single": 2.0, "small": 1.4, "apartments": 1.0}
OFFSTREET_PER_UNIT = {"Single": 2.0, "small": 1.0, "apartments": 0.8}
APT_FLOORS, APT_M2_PER_UNIT = 3.0, 85.0
RES_ONSTREET_EXTRA = 0.15        # share of cars parked on street even when off-street space exists
COMMUTE_DRIVE = 0.7              # share of resident workers who drive to work
# CAL: share of street-parked resident cars that leave on a weekday commute (many curb cars sit all day)
ONSTREET_COMMUTE_SHARE = float(os.environ.get("PARKSIM_COMMUTE_SHARE", 0.5))
ERRAND_PROB = {"weekday": 0.35, "weekend": 0.6}
# --- commuters (CAL) ---
COMMUTER_DRIVE = 0.7
COMMUTER_CURB = float(os.environ.get("PARKSIM_COMMUTER_CURB", 0.05))
# --- visitors (CAL) ---
VISITOR_SCALE = float(os.environ.get("PARKSIM_VISITOR_SCALE", 0.25))  # people present at 100% busyness per sqrt(review)
# CAL: extra visitor curb demand 17:00-02:00 (evening trips are more often by car, and meters go free after hours)
VISITOR_EVENING = float(os.environ.get("PARKSIM_VISITOR_EVENING", 3.0))
EVENING_HOURS = [17, 18, 19, 20, 21, 22, 23, 0, 1]
VISITOR_DRIVE, CAR_OCC = 0.6, 1.3
LOT_CURB_SHARE, NO_LOT_CURB_SHARE = 0.1, 0.8
BIG_BOX = {"Shopping mall", "Supermarket", "Department store", "Home improvement store", "Grocery store",
           "Warehouse store", "Hospital", "Movie theater", "Stadium", "University", "Amusement park"}
DEFAULT_DWELL_MIN = 45.0

RNG = np.random.default_rng(int(os.environ.get("PARKSIM_SEED", 0)))


def _in(lat, lon, bbox, margin=0.002):
    return (bbox[0] - margin <= lat) & (lat <= bbox[1] + margin) & (bbox[2] - margin <= lon) & (lon <= bbox[3] + margin)


def residents(bbox, weekend=False):
    """Resident cars needing curb space: home point, and away intervals (minutes) for the day."""
    rows = [r for r in csv.DictReader(open(LODES_DIR / "dtla_parcels.csv")) if r["CENTER_LAT"]]
    lat = np.array([float(r["CENTER_LAT"]) for r in rows])
    lon = np.array([float(r["CENTER_LON"]) for r in rows])
    keep = _in(lat, lon, bbox)
    rows = [r for r, k in zip(rows, keep) if k]
    lat, lon = lat[keep], lon[keep]
    pxy = xy(lat, lon)
    # apartment unit counts from the largest building footprint at the parcel
    bdb = sqlite3.connect(f"file:{BUILDINGS_DB}?mode=ro", uri=True)
    B = np.array(bdb.execute("select min_lat,max_lat,min_lon,max_lon from buildings_rtree where max_lat>=? and min_lat<=? and "
                             "max_lon>=? and min_lon<=?", (lat.min(), lat.max(), lon.min(), lon.max())).fetchall())
    bxy = xy((B[:, 0] + B[:, 1]) / 2, (B[:, 2] + B[:, 3]) / 2)
    barea = (B[:, 1] - B[:, 0]) * M_LAT * (B[:, 3] - B[:, 2]) * M_LON
    btree = cKDTree(bxy)
    kind, units = [], []
    for r, p in zip(rows, pxy):
        u = r["UseDescription"]
        if u in UNITS:
            kind.append("Single" if u == "Single" else "small")
            units.append(UNITS[u])
        elif u == "Five or more apartments":
            idx = btree.query_ball_point(p, 25)
            fp = max((barea[i] for i in idx), default=600.0)
            kind.append("apartments")
            units.append(max(5.0, min(250.0, fp * APT_FLOORS / APT_M2_PER_UNIT)))
        else:
            kind.append(None)
            units.append(0)
    units = np.array(units, float)
    res = np.array([k is not None for k in kind])
    # scale units so area totals match LODES workers-by-home (~1.25 residents' cars per worker household proxy)
    blk, home, _ = lodes_blocks()
    bl = [k for k in home if k in blk and _in(np.array(blk[k][0]), np.array(blk[k][1]), bbox, 0)]
    workers = sum(home[k] for k in bl)
    hh_est = workers / 1.3
    scale = hh_est / max(units[res].sum(), 1)
    cars_home, cars_onstreet = [], []
    for k, u in zip(kind, units):
        if k is None:
            cars_home.append(0)
            cars_onstreet.append(0)
            continue
        veh = u * scale * VEH_PER_UNIT[k]
        off = u * scale * OFFSTREET_PER_UNIT[k]
        cars_home.append(veh)
        cars_onstreet.append(max(0.0, veh - off) + RES_ONSTREET_EXTRA * min(veh, off))
    n = RNG.poisson(np.array(cars_onstreet))
    homes = np.repeat(pxy, n, axis=0)
    # day schedule: commute (share of cars = drivers among workers) and errands
    worker_share = ONSTREET_COMMUTE_SHARE
    away = resident_trips(len(homes), weekend, worker_share)
    return dict(xy=homes, away=away, units=units.sum() * scale, onstreet_cars=len(homes), worker_share=worker_share)


def resident_trips(n, weekend, worker_share):
    """Away intervals (minutes) for n resident cars: a commute for drivers among workers, plus errands."""
    away = []
    for _ in range(n):
        trips = []
        if not weekend and RNG.random() < worker_share:
            dep = RNG.normal(7.5 * 60, 60)
            ret = RNG.normal(17.75 * 60, 90)
            trips.append((dep, max(dep + 120, ret)))
        if RNG.random() < ERRAND_PROB["weekend" if weekend else "weekday"]:
            s = RNG.uniform(9 * 60, 20 * 60)
            trips.append((s, s + RNG.uniform(45, 180)))
        away.append(trips)
    return away


def commuters(bbox, weekend=False):
    """Inbound commuters parking on the curb near their workplace (LODES work blocks): (point, arrive, leave)."""
    if weekend:
        return dict(xy=np.zeros((0, 2)), arrive=np.zeros(0), leave=np.zeros(0))
    blk, _, work = lodes_blocks()
    keys = [k for k in work if k in blk and _in(np.array(blk[k][0]), np.array(blk[k][1]), bbox)]
    n = RNG.poisson(np.array([work[k] for k in keys], float) * COMMUTER_DRIVE * COMMUTER_CURB)
    pts = np.repeat(xy([blk[k][0] for k in keys], [blk[k][1] for k in keys]), n, axis=0)
    arrive = RNG.normal(8.5 * 60, 50, len(pts))
    return dict(xy=pts, arrive=arrive, leave=arrive + RNG.normal(9 * 60, 45, len(pts)))


def offstreet_lots(bbox):
    """Points of known off-street parking: assessor 'Parking Lots' parcels and operator lots."""
    pts = []
    for r in csv.DictReader(open(LODES_DIR / "dtla_parcels.csv")):
        if r["CENTER_LAT"] and "Parking" in (r["UseDescription"] or ""):
            pts.append((float(r["CENTER_LAT"]), float(r["CENTER_LON"])))
    db = sqlite3.connect(f"file:{LA_PARKING_DB / 'park_smarter_meters.db'}?mode=ro", uri=True)
    for t in ("la_city_parking_lots", "laz_locations", "spplus_locations", "ace_locations"):
        pts += db.execute(f"select lat, lon from {t} where lat is not null").fetchall()
    pts = np.array(pts, float)
    return xy(pts[:, 0], pts[:, 1])[_in(pts[:, 0], pts[:, 1], bbox)]


def visitors(bbox, weekday):
    """Visitor car arrivals for one day: (point, arrive_min, dwell_min). Arrivals = presence / dwell."""
    pl = place_profiles()
    keep = _in(pl["lat"], pl["lon"], bbox)
    lots = cKDTree(offstreet_lots(bbox))
    pxy = xy(pl["lat"][keep], pl["lon"][keep])
    prof = pl["profile"][keep][:, weekday, :]                          # 24 hourly busyness (0-1)
    dwell = np.where(np.isfinite(pl["dwell_min"][keep]), pl["dwell_min"][keep], DEFAULT_DWELL_MIN)
    dwell = np.clip(dwell, 10, 240)
    cats = [c for c, k in zip(pl["category"], keep) if k]
    near_lot = np.array([len(lots.query_ball_point(p, 60)) > 0 for p in pxy])
    curb_share = np.where(near_lot | np.array([c in BIG_BOX for c in cats]), LOT_CURB_SHARE, NO_LOT_CURB_SHARE)
    present = VISITOR_SCALE * np.sqrt(pl["reviews"][keep])[:, None] * prof   # people present each hour
    cars_per_hour = present / (dwell[:, None] / 60) * VISITOR_DRIVE / CAR_OCC * curb_share[:, None]
    cars_per_hour[:, EVENING_HOURS] *= VISITOR_EVENING
    n = RNG.poisson(cars_per_hour)
    pts, arr, dw = [], [], []
    for i, h in zip(*np.nonzero(n)):
        k = n[i, h]
        pts.append(np.repeat(pxy[i][None], k, 0))
        arr.append(h * 60 + RNG.uniform(0, 60, k))
        dw.append(RNG.exponential(dwell[i], k).clip(5, 480))
    return dict(xy=np.vstack(pts), arrive=np.concatenate(arr), dwell=np.concatenate(dw), places=int(keep.sum()))
