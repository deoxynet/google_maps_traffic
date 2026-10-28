#!/usr/bin/env python3
"""Build curb supply for the parking simulator: block faces, capacity, and weekly rule calendars.

Block faces come from OSM street segments (data/osm_roads.db), chained into blocks between intersections, split
to <= 200 m, one face per side. No dependency on web/graph.json, so any bbox in LA works.

Capacity = (length - 2 x 7 m intersection setback) / 6.1 m x (1 - share of curb that tickets show cannot be parked
on) x a residual usable share. Ticketed driveways (4.5 m), hydrants (9.1 m), red curb (6 m) and bus zones (25 m)
are clustered by position along the curb. Their share is pooled over same-type streets within 500 m: per-face
subtraction is right on average but noisy, because tickets are written where parking pressure is high. Checked against
137 metered faces (1 meter = 1 legal space): mean error 5.2 spaces vs 10.5 for pure geometry.
Rules are 15-minute slots over a week (7 x 96, Monday first):
  - street sweeping: posted LA routes (la-parking street_sweeping.db), only in its weeks of the month
  - permit districts: inferred from preferential-parking citations (method validated on sweeping, 97%)
  - meters: paid hours, max stay, and 'No Parking' tow-away windows from the meter inventory snapshot
  - no stopping / no parking windows and loading zones: hours in which those tickets are written on the face

Usage: python3 scripts/parking_sim/supply.py [--bbox lat0,lat1,lon0,lon1]
Writes data/parking_sim/supply.npz
"""
import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import shapely
from scipy.spatial import cKDTree
from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "curb"))
from common import BASE_DIR, CLASSES, CURB_DIR, LA_PARKING_DB, M_LAT, M_LON, ROADS_DB, xy  # noqa: E402

OUT_DIR = BASE_DIR / "data" / "parking_sim"
TEST_BBOX = (34.035, 34.0725, -118.345, -118.280)  # K-town / Mid-Wilshire, Pico to ~3rd St (parcel data ends 34.073)
SLOTS = 96            # 15-minute slots per day
SPACE_M = 6.1         # curb length per parallel space
SETBACK_M = 7.0       # no parking near each intersection
MAX_BLOCK_M = 200.0
# distance from centerline to the middle of the parking lane, by class (from the satellite offset histograms)
CURB_OFFSET = {"residential": 4.5, "unclassified": 4.5, "living_street": 4.0, "tertiary": 6.0, "secondary": 8.0, "primary": 9.0}
TOPOLOGY_ONLY = ("trunk", "trunk_link", "motorway", "motorway_link", "primary_link", "secondary_link", "tertiary_link")
# metres of curb lost per distinct ticketed address, by ticket kind
EXCLUSION_M = {"driveway": 4.5, "hydrant": 9.1, "red": 6.0, "bus": 25.0}
LOADING_M = {"white": 7.0, "yellow": 9.0}
# CAL: share of what is left that is really parkable (unticketed driveways, red curb, alleys)
USABLE = {"residential": 0.85, "unclassified": 0.85, "living_street": 0.7, "tertiary": 0.8, "secondary": 0.75, "primary": 0.7}


def build_faces(bbox):
    """Chain OSM segments into blocks between intersections, split long blocks, and make one face per side."""
    db = sqlite3.connect(f"file:{ROADS_DB}?mode=ro", uri=True)
    classes = CLASSES + TOPOLOGY_ONLY
    rows = db.execute(f"select start_lat,start_lon,end_lat,end_lon,highway from road_segments where highway in "
                      f"({','.join('?' * len(classes))}) and start_lat between ? and ? and start_lon between ? and ?",
                      (*classes, bbox[0], bbox[1], bbox[2], bbox[3])).fetchall()
    key = lambda la, lo: (round(la, 7), round(lo, 7))
    A = [key(r[0], r[1]) for r in rows]
    B = [key(r[2], r[3]) for r in rows]
    hw = [r[4] for r in rows]
    adj = defaultdict(list)
    for s, (a, b) in enumerate(zip(A, B)):
        adj[a].append(s)
        adj[b].append(s)
    seen = np.zeros(len(rows), bool)
    node_id = {}
    nid = lambda k: node_id.setdefault(k, len(node_id))

    def walk(node, came):
        """Follow degree-2 nodes with the same class; return the nodes visited after `node`."""
        out = []
        while len(adj[node]) == 2:
            nxt = [t for t in adj[node] if t != came][0]
            if seen[nxt] or hw[nxt] != hw[came]:
                break
            seen[nxt] = True
            node = B[nxt] if A[nxt] == node else A[nxt]
            out.append(node)
            came = nxt
        return out

    faces = []
    for s in range(len(rows)):
        if seen[s] or hw[s] not in CLASSES:
            continue
        seen[s] = True
        back = walk(A[s], s)
        fwd = walk(B[s], s)
        chain = back[::-1] + [A[s], B[s]] + fwd
        ll = np.array(chain)
        m = xy(ll[:, 0], ll[:, 1])
        seglen = np.hypot(*np.diff(m, axis=0).T)
        total = seglen.sum()
        if total < 15:
            continue
        cls = CLASSES.index(hw[s])
        # split into equal pieces <= MAX_BLOCK_M along the polyline
        k = max(1, int(math.ceil(total / MAX_BLOCK_M)))
        cum = np.r_[0, np.cumsum(seglen)]
        cuts = np.linspace(0, total, k + 1)
        for j in range(k):
            sel = (cum > cuts[j]) & (cum < cuts[j + 1])
            pa = np.array([np.interp(cuts[j], cum, m[:, 0]), np.interp(cuts[j], cum, m[:, 1])])
            pb = np.array([np.interp(cuts[j + 1], cum, m[:, 0]), np.interp(cuts[j + 1], cum, m[:, 1])])
            pts = np.vstack([pa, m[sel], pb])
            L = cuts[j + 1] - cuts[j]
            mid = np.array([np.interp((cuts[j] + cuts[j + 1]) / 2, cum, m[:, 0]), np.interp((cuts[j] + cuts[j + 1]) / 2, cum, m[:, 1])])
            d = pb - pa
            d = d / max(np.hypot(*d), 1e-9)
            ends = (j == 0, j == k - 1)  # setbacks only at real intersections
            block = len(faces) // 2
            fn = nid(chain[0]) if j == 0 else -1
            tn = nid(chain[-1]) if j == k - 1 else -1
            off = CURB_OFFSET[CLASSES[cls]]
            for side, sgn in (("L", 1), ("R", -1)):
                normal = np.array([-d[1], d[0]]) * sgn
                # offset each vertex along its own local normal so curved blocks stay on the curb
                tang = np.gradient(pts, axis=0)
                tang /= np.maximum(np.hypot(*tang.T), 1e-9)[:, None]
                nrm = np.c_[-tang[:, 1], tang[:, 0]] * sgn
                faces.append(dict(edge=block, side=side, length_m=L, mid=mid, normal=normal, dir=d, cls=cls,
                                  center=pts, poly=pts + nrm * off, ends=sum(ends), from_node=fn, to_node=tn))
    return faces


def curb_samples(S, step=5.0):
    """Points along each saved face's curb line (from supply.npz) -> (points, face index)."""
    pts, own = [], []
    ends = list(S["poly_start"][1:]) + [len(S["poly"])]
    for f, (a, b) in enumerate(zip(S["poly_start"], ends)):
        c = xy(S["poly"][a:b, 0], S["poly"][a:b, 1])
        t = np.gradient(c, axis=0)
        t /= np.maximum(np.hypot(*t.T), 1e-9)[:, None]
        sgn = 1 if S["side"][f] == "L" else -1
        line = c + np.c_[-t[:, 1], t[:, 0]] * sgn * CURB_OFFSET[CLASSES[S["cls"][f]]]
        for p, q in zip(line[:-1], line[1:]):
            k = max(1, int(np.hypot(*(q - p)) // step))
            for u in np.linspace(0, 1, k, endpoint=False):
                pts.append(p + (q - p) * u)
                own.append(f)
    return np.array(pts), np.array(own)


def face_samples(faces, step=10.0):
    """Points every ~10 m along each face's curb line -> (points, face index)."""
    pts, owner = [], []
    for i, f in enumerate(faces):
        P = f["poly"]
        for a, b in zip(P[:-1], P[1:]):
            k = max(1, int(np.hypot(*(b - a)) // step))
            for t in np.linspace(0, 1, k, endpoint=False):
                pts.append(a + (b - a) * t)
                owner.append(i)
        pts.append(P[-1])
        owner.append(i)
    return np.array(pts), np.array(owner)


def week_slots(day, start_h, end_h):
    s, e = int(round(start_h * 4)), int(round(end_h * 4))
    return [(day, q) for q in range(max(s, 0), min(e, SLOTS))]


def hour_windows(H, min_total=10, share=0.04):
    """Weekly (weekday-block, weekend-block) restricted hours from a (2, 24) ticket-hour histogram."""
    slots = []
    for weekend, days in ((0, range(5)), (1, range(5, 7))):
        if H[weekend].sum() < min_total:
            continue
        hours = H[weekend] >= max(2, share * H[weekend].sum())
        on = np.where(hours)[0]
        for a, b in zip(on[:-1], on[1:]):  # close gaps of <= 2 h (patrols skip hours)
            if b - a <= 3:
                hours[a:b] = True
        slots += [(wd, hr * 4 + q) for wd in days for hr in range(24) if hours[hr] for q in range(4)]
    return slots


def ticket_curb(faces, samples):
    """Exclusions and time-window rules per face from curb tickets (data/curb/citations_curb_2023plus.npz)."""
    pts, owner = samples
    z = np.load(CURB_DIR / "citations_curb_2023plus.npz")
    C, kinds = z["data"], list(z["kinds"])
    d, j = cKDTree(pts).query(xy(C[:, 0], C[:, 1]), distance_upper_bound=12)
    ok = np.isfinite(d)
    C, f_of = C[ok], owner[j[ok]]
    loading = defaultdict(lambda: defaultdict(set))
    hours = defaultdict(lambda: np.zeros((len(kinds), 2, 24)))
    for row, f in zip(C, f_of):
        hours[f][int(row[2]), int(row[3] >= 5), int(row[4])] += 1
        if kinds[int(row[2])] in LOADING_M:
            loading[f][kinds[int(row[2])]].add(int(row[5]))
    # Exclusions: dedupe by position along the curb, not by address text. One red curb or bus stop gets
    # geocoded to many address variants; tickets within 3 m of each other are one feature.
    static = np.isin(C[:, 2], [kinds.index(k) for k in EXCLUSION_M])
    lines = [shapely.LineString(f["poly"]) for f in faces]
    pos = shapely.line_locate_point(np.array(lines, dtype=object)[f_of[static]],
                                    shapely.points(xy(C[static, 0], C[static, 1])))
    excl_m = np.zeros(len(faces))
    lens = np.array([EXCLUSION_M.get(k, 0.0) for k in kinds])
    fs, ks = f_of[static], C[static, 2].astype(int)
    order = np.lexsort((pos, fs))
    fs, pos, ks = fs[order], pos[order], ks[order]
    start = 0
    clusters = []  # (face, start m, end m, kind with the longest exclusion, tickets)
    for i in range(1, len(fs) + 1):
        if i == len(fs) or fs[i] != fs[start] or pos[i] - pos[i - 1] > 3.0:
            k = ks[start:i][np.argmax(lens[ks[start:i]])]
            clusters.append((fs[start], pos[start], pos[i - 1], k, i - start))
            start = i
    clusters = np.array(clusters).reshape(-1, 5)
    for f, a, b, k, n in clusters:
        f = int(f)
        L = faces[f]["length_m"]
        # Corner red curb / driveways inside the intersection setback are already excluded
        lo, hi = (SETBACK_M if faces[f]["from_node"] >= 0 else 0.0), (L - SETBACK_M if faces[f]["to_node"] >= 0 else L)
        a2, b2 = max(a - 1.0, lo), min(max(b + 1.0, a + lens[int(k)] - 1.0), hi)
        if b2 > a2:
            excl_m[f] += b2 - a2
    geom_m = np.array([max(0.0, f["length_m"] - f["ends"] * SETBACK_M) for f in faces])
    excl_m = np.minimum(excl_m, geom_m)
    out = dict(excl_m=excl_m, clusters=clusters, nostop=defaultdict(list), loading=defaultdict(list), double_night=np.zeros(len(faces)))
    ki = {k: i for i, k in enumerate(kinds)}
    for f, H in hours.items():
        for k in ("nostop", "nopark"):
            out["nostop"][f] += hour_windows(H[ki[k]])
        for k, m in LOADING_M.items():
            if loading[f][k]:
                spaces = len(loading[f][k]) * m / SPACE_M
                out["loading"][f] += [(s, spaces) for s in hour_windows(H[ki[k]], min_total=5)]
    # double parking at night (21:00-06:00): a saturation signal that plays no part in capacity, so it can
    # validate the simulator without circularity (red/hydrant/driveway tickets also set the exclusions)
    night = (C[:, 4] >= 21) | (C[:, 4] < 6)
    out["double_night"] = np.bincount(f_of[night & (C[:, 2] == ki["double"])], minlength=len(faces)).astype(float)
    out["double_day"] = np.bincount(f_of[~night & (C[:, 2] == ki["double"])], minlength=len(faces)).astype(float)
    return out


def pooled_exclusion_share(curb_xy, cls, geom_m, excl_m, radius=500.0):
    """Ticket-excluded share of curb, pooled over faces of the same type (residential vs arterial) within radius."""
    grp = np.isin(cls, [0, 1, 2]).astype(int)
    share = np.zeros(len(cls))
    for i, idx in enumerate(cKDTree(curb_xy).query_ball_point(curb_xy, radius)):
        idx = np.array(idx)
        idx = idx[grp[idx] == grp[i]]
        share[i] = excl_m[idx].sum() / max(geom_m[idx].sum(), 1.0)
    return np.clip(share, 0, 0.9)


def sweeping(faces, curb_ll):
    """Per face: (weekday, slot) list and allowed weeks of the month, from posted LA routes."""
    db = sqlite3.connect(f"file:{LA_PARKING_DB / 'street_sweeping.db'}?mode=ro", uri=True)
    R = db.execute("select route, start_time, end_time, sweep_days, week_1, week_2, week_3, week_4, is_weekly, geometry "
                   "from routes where city='la' and geometry is not null and start_time is not null").fetchall()
    groups = defaultdict(list)
    for r in R:
        groups[r[0].split()[0]].append(r)
    names = list(groups)
    polys = [shapely.union_all([shapely.make_valid(shape(json.loads(r[9]))) for r in groups[n]]) for n in names]
    tree = shapely.STRtree(polys)
    fi, gi = tree.query(shapely.points(curb_ll[:, 1], curb_ll[:, 0]), predicate="intersects")
    hm = lambda s: int(s[:2]) + int(s[3:5]) / 60
    sweep = {}
    for f, gidx in zip(fi, gi):
        rs = sorted(groups[names[gidx]], key=lambda r: (json.loads(r[3]) or [9])[0])
        # The data does not say which side is swept on which day. Convention: faces whose outward normal points
        # north or east take the earlier day of the pair.
        n = faces[f]["normal"]
        north_or_east = n[1] > 0 if abs(n[1]) >= abs(n[0]) else n[0] > 0
        r = rs[0] if (north_or_east or len(rs) == 1) else rs[-1]
        weeks = [1, 2, 3, 4, 5] if r[8] else [i + 1 for i, fl in enumerate(r[4:8]) if fl]
        sweep[f] = dict(slots=[s for d in json.loads(r[3]) or [] for s in week_slots(d, hm(r[1]), hm(r[2]))],
                        weeks=weeks, route=r[0])
    return sweep


def permit_districts(faces, samples):
    """Permit-district faces and their restricted hours, read off preferential-parking citations.

    Only non-permit cars on permit streets get these citations, so a face with a few of them is a permit street.
    Hours are pooled over the surrounding district (citations within 150 m) because one face has too few."""
    pts, owner = samples
    C = np.load(CURB_DIR / "citations_ppd_2023plus.npy")  # lat, lon, weekday, hour
    cxy = xy(C[:, 0], C[:, 1])
    d, j = cKDTree(pts).query(cxy, distance_upper_bound=12)
    ok = np.isfinite(d)
    per_face = np.bincount(owner[j[ok]], minlength=len(faces))
    ctree = cKDTree(cxy)
    ppd = {}
    for f in np.where(per_face >= 3)[0]:
        H = np.zeros((2, 24))
        for c in ctree.query_ball_point(faces[f]["mid"], 150):
            H[int(C[c, 2] >= 5), int(C[c, 3])] += 1
        slots = hour_windows(H, share=0.03)
        if slots:
            ppd[f] = slots
    return ppd


def meters(faces, samples):
    """Per face: paid slots, max stay (min), and 'No Parking' slots from the meter zones along it."""
    db = sqlite3.connect(f"file:{LA_PARKING_DB / 'park_smarter_meters.db'}?mode=ro", uri=True)
    rows = db.execute("select lat, lon, spaces_json from meters where customer_name like 'Los Angeles%' and lat is not null").fetchall()
    pts, owner = samples
    d, j = cKDTree(pts).query(xy([r[0] for r in rows], [r[1] for r in rows]), distance_upper_bound=12)
    out = {}
    for (lat, lon, sj), dist, jj in zip(rows, d, j):
        if not np.isfinite(dist):
            continue
        f = owner[jj]
        if f in out:
            out[f]["meter_spaces"] += len(json.loads(sj))
            continue
        try:
            pol = json.loads(sj)[0]["Policies"]
        except Exception:
            continue
        paid, maxstay, nopark = [], {}, []
        for p in pol:
            day = p["DayNumber"]  # Sunday=1 ... Saturday=7; snapshot holds Friday (6) and Saturday (7)
            weekdays = range(5) if day == 6 else ([5] if day == 7 else [])
            s = int(p["StartDateTime"][11:13]) + int(p["StartDateTime"][14:16]) / 60
            e = int(p["EndDateTime"][11:13]) + int(p["EndDateTime"][14:16]) / 60 or 24
            for wd in weekdays:
                sl = week_slots(wd, s, e if e > s else 24)
                if p["RateType"] == "Hour":
                    paid += sl
                    for x in sl:
                        maxstay[x] = p.get("MaxTime") or 0
                elif p["RateType"] == "No Parking":
                    nopark += sl
        out[f] = dict(paid=paid, maxstay=maxstay, nopark=nopark, meter_spaces=len(json.loads(sj)))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", default=",".join(map(str, TEST_BBOX)))
    args = ap.parse_args()
    bbox = tuple(map(float, args.bbox.split(",")))
    mg = 0.006  # ~600 m of extra supply around the core so edge demand can find spaces
    faces = build_faces((bbox[0] - mg, bbox[1] + mg, bbox[2] - mg, bbox[3] + mg))
    n = len(faces)
    cls = np.array([f["cls"] for f in faces])
    curb_xy = np.array([f["mid"] + f["normal"] * CURB_OFFSET[CLASSES[f["cls"]]] for f in faces])
    curb_ll = np.c_[curb_xy[:, 1] / M_LAT, curb_xy[:, 0] / M_LON]
    samples = face_samples(faces)
    tk = ticket_curb(faces, samples)
    geom = np.array([max(0.0, f["length_m"] - f["ends"] * SETBACK_M) / SPACE_M for f in faces])
    usable = np.array([USABLE[CLASSES[c]] for c in cls])
    share = pooled_exclusion_share(curb_xy, cls, geom * SPACE_M, tk["excl_m"])
    cap = geom * (1 - share) * usable
    sw = sweeping(faces, curb_ll)
    ppd = permit_districts(faces, samples)
    mt = meters(faces, samples)
    nopark = np.zeros((n, 7, SLOTS), bool)
    permit = np.zeros((n, 7, SLOTS), bool)
    paid = np.zeros((n, 7, SLOTS), bool)
    maxstay = np.zeros((n, 7, SLOTS), np.int16)
    loading = np.zeros((n, 7, SLOTS), np.float32)   # spaces lost to loading zones in that slot
    sweep_slots = np.zeros((n, 7, SLOTS), bool)
    sweep_weeks = np.zeros((n, 5), bool)
    for f, s in sw.items():
        for wd, q in s["slots"]:
            sweep_slots[f, wd, q] = True
        sweep_weeks[f, [w - 1 for w in s["weeks"]]] = True
    for f, sl in ppd.items():
        for wd, q in sl:
            permit[f, wd, q] = True
    for f, m in mt.items():
        for wd, q in m["paid"]:
            paid[f, wd, q] = True
        for (wd, q), v in m["maxstay"].items():
            maxstay[f, wd, q] = v
        for wd, q in m["nopark"]:
            nopark[f, wd, q] = True
    for f, sl in tk["nostop"].items():
        for wd, q in sl:
            nopark[f, wd, q] = True
    for f, sl in tk["loading"].items():
        for (wd, q), sp in sl:
            loading[f, wd, q] = sp
    core = (curb_ll[:, 0] >= bbox[0]) & (curb_ll[:, 0] <= bbox[1]) & (curb_ll[:, 1] >= bbox[2]) & (curb_ll[:, 1] <= bbox[3])
    sweep_route = np.array([sw[f]["route"] if f in sw else "" for f in range(n)])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    poly_ll = [np.c_[f["center"][:, 1] / M_LAT, f["center"][:, 0] / M_LON].round(6) for f in faces]
    np.savez_compressed(OUT_DIR / "supply.npz", bbox=np.array(bbox), core=core, sweep_route=sweep_route,
                        edge=np.array([f["edge"] for f in faces]), side=np.array([f["side"] for f in faces]),
                        length_m=np.array([f["length_m"] for f in faces]), cls=cls, capacity=cap, capacity_geom=geom,
                        excl_m=tk["excl_m"], excl_clusters=tk["clusters"], night_double=tk["double_night"], day_double=tk["double_day"], xy=curb_xy, latlon=curb_ll,
                        from_node=np.array([f["from_node"] for f in faces]), to_node=np.array([f["to_node"] for f in faces]),
                        poly_start=np.cumsum([0] + [len(p) for p in poly_ll])[:-1], poly=np.vstack(poly_ll),
                        nopark=nopark, permit=permit, paid=paid, maxstay=maxstay, loading=loading,
                        sweep_slots=sweep_slots, sweep_weeks=sweep_weeks,
                        meter_spaces=np.array([mt[f]["meter_spaces"] if f in mt else 0 for f in range(n)]))
    c = core
    print(f"{n} block faces ({n // 2} blocks) incl. margin; core {bbox}: {(c & (cap >= 0.5)).sum()} faces with parking")
    print(f"  core capacity {cap[c].sum():,.0f} spaces (geometric {geom[c].sum():,.0f}; pooled ticket exclusion share "
          f"median {np.median(share[c]):.0%})")
    print("  core by class: " + ", ".join(f"{k} {cap[c & (cls == i)].sum():,.0f}" for i, k in enumerate(CLASSES) if (c & (cls == i)).any()))
    print(f"  sweeping {len(sw)} faces; permit districts {len(ppd)}; meters {len(mt)}; ticket no-stop/no-park windows "
          f"{len(tk['nostop'])}; loading zones {len(tk['loading'])}")


if __name__ == "__main__":
    main()
