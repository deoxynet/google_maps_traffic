#!/usr/bin/env python3
"""Extract LA City citations of one kind from la-parking's warehouse into a compact array.

Usage: python3 scripts/curb/extract_citations.py ppd|sweep|curb
  ppd   -> data/curb/citations_ppd_2023plus.npy    (lat, lon, weekday, hour)
  sweep -> data/curb/citations_sweep_2023plus.npy  (lat, lon, weekday, hour.frac, day_of_month, house_no_parity)
  curb  -> data/curb/citations_curb_2023plus.npz   lat, lon, kind (CURB_KINDS index), weekday, hour, address id, day
           kinds mark curb that cannot be parked on (driveways, hydrants, red/bus/loading zones), time-window
           bans (no stopping / no parking), and saturation signals (double parking)
"""
import re
import sqlite3
import sys
from datetime import date

import numpy as np

from common import CURB_DIR, LA_PARKING_DB

KINDS = {"ppd": ("PREFERENTIAL PARKING",), "sweep": ("NO PARK/STREET CLEAN", "8069B NO PARK ST CLN")}
CURB_KINDS = {
    "driveway": ("BLOCKING DRIVEWAY",),
    "hydrant": ("FIRE HYDRANT",),
    "red": ("RED ZONE",),
    "bus": ("EXCLUSIVE FOR BUSES",),
    "white": ("WHITE ZONE",),
    "yellow": ("YELLOW ZONE",),
    "nostop": ("NO STOP/STANDING", "NO STOP/STAND", "STOP/STAND PROHIBIT", "NO STOPPING/ANTI-GRIDLOCK ZONE"),
    "nopark": ("NO PARKING",),
    "double": ("DOUBLE PARKING",),
    "timelimit": ("PARKED OVER TIME LIMIT",),
}


def extract_curb(db, city):
    names = list(CURB_KINDS)
    lookup = {d: i for i, k in enumerate(names) for d in CURB_KINDS[k]}
    descs = list(lookup)
    rows = db.execute(
        f"select violation_desc, latitude, longitude, issue_date, issue_time, upper(trim(coalesce(nullif(street_address,''), location_text))) "
        f"from citations where city_id=? and violation_desc in ({','.join('?' * len(descs))}) and issue_date>='2023-01-01' "
        f"and issue_time is not null and latitude between 33.6 and 34.5 and longitude between -118.8 and -118.0",
        (city, *descs)).fetchall()
    addr_ids = {}
    out = np.zeros((len(rows), 7))
    for i, (v, la, lo, d, tm, addr) in enumerate(rows):
        y, m, dd = map(int, d.split("-"))
        dt = date(y, m, dd)
        out[i] = (la, lo, lookup[v], dt.weekday(), int(tm[:2]) + int(tm[3:5]) / 60,
                  addr_ids.setdefault(addr or f"?{la:.5f},{lo:.5f}", len(addr_ids)), dt.toordinal())
    dest = CURB_DIR / "citations_curb_2023plus.npz"
    np.savez_compressed(dest, data=out, kinds=np.array(names))
    print(f"{len(out):,} citations, {len(addr_ids):,} distinct addresses -> {dest}")
    for i, k in enumerate(names):
        print(f"  {k:10s} {int((out[:, 2] == i).sum()):9,d}")


def main():
    kind = sys.argv[1]
    db = sqlite3.connect(f"file:{LA_PARKING_DB / 'parking.db'}?mode=ro", uri=True)
    city = db.execute("select city_id from cities where name='Los Angeles'").fetchone()[0]
    if kind == "curb":
        return extract_curb(db, city)
    descs = KINDS[kind]
    rows = db.execute(
        f"select latitude, longitude, issue_date, issue_time, coalesce(street_address, location_text) from citations "
        f"where city_id=? and violation_desc in ({','.join('?' * len(descs))}) and issue_date>='2023-01-01' "
        f"and issue_time is not null and latitude between 33.6 and 34.5 and longitude between -118.8 and -118.0",
        (city, *descs)).fetchall()
    num = re.compile(r"^\s*(\d+)\s")
    out = []
    for la, lo, d, tm, addr in rows:
        y, m, dd = map(int, d.split("-"))
        wd = date(y, m, dd).weekday()
        if kind == "ppd":
            out.append((la, lo, wd, int(tm[:2])))
        else:
            mm = num.match(addr or "")
            if mm:
                out.append((la, lo, wd, int(tm[:2]) + int(tm[3:5]) / 60, dd, int(mm.group(1)) % 2))
    dest = CURB_DIR / f"citations_{kind}_2023plus.npy"
    np.save(dest, np.array(out))
    print(f"{len(out):,} citations -> {dest}")


if __name__ == "__main__":
    main()
