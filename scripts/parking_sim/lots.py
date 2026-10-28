#!/usr/bin/env python3
"""Public off-street parking near the simulated area, as fallbacks for the search optimizer.

Sources:
  - la-parking park_smarter_meters.db: LA City lots (posted hourly rate, daily max, hours), LAZ, SP+, ACE and ABM
    facilities (location, capacity; rates mostly not collected). SP+ "(M) ..." entries are monthly-only and dropped.
  - OSM amenity=parking (data/parking_sim/osm_ways.json "parking"): access=yes/permissive or fee=yes as public;
    untagged multi-storey garages as "likely public" (verified=false). Private, customers, permit and untagged
    surface lots are left out.
Lots within 40 m of each other are merged, keeping the one with a posted rate. Writes data/parking_sim/lots.json:
  [{name, operator, lat, lon, spaces, rate_per_hr, daily_max, hours, open: [7][24] or null, verified}]
Usage: python3 scripts/parking_sim/lots.py
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0] / "curb"))
from common import LA_PARKING_DB, xy  # noqa: E402
from supply import OUT_DIR  # noqa: E402

DAY_NAMES = {"M": 0, "Mon": 0, "Tu": 1, "Tue": 1, "W": 2, "Wed": 2, "Th": 3, "Thu": 3, "F": 4, "Fri": 4, "Sat": 5, "Sa": 5, "Sun": 6, "Su": 6}


def per_hour(text):
    """'$1.50 / 15 Min' -> 6.0 ; '$1.00 / Hr' -> 1.0 ; 'Free' -> 0.0"""
    if not text or text.strip() in ("--", ""):
        return None
    if "free" in text.lower():
        return 0.0
    m = re.search(r"\$([\d.]+)\s*/\s*(\d*)\s*(min|hr|hour)", text, re.I)
    if not m:
        return None
    amount, n, unit = float(m.group(1)), float(m.group(2) or 1), m.group(3).lower()
    return amount * (60 / n if unit == "min" else 1 / n)


def daily_max(text):
    m = re.fullmatch(r"\s*\$([\d.]+)\s*", text or "")
    return float(m.group(1)) if m else None


def hour_of(t):
    t = t.strip().upper()
    if t == "MIDNIGHT":
        return 24
    if t == "NOON":
        return 12
    m = re.match(r"(\d+)(?::(\d+))?\s*(AM|PM)", t)
    h = int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0)
    return h


def open_grid(hours):
    """'7 AM-9 PM M-Sat, 9 AM-5 PM Sun free' -> 7x24 bool (None when unparseable)."""
    if not hours:
        return None
    grid = np.zeros((7, 24), bool)
    parsed = False
    for part in hours.split(","):
        m = re.search(r"(\d+(?::\d+)?\s*[AP]M|Noon|Midnight)\s*-\s*(\d+(?::\d+)?\s*[AP]M|Noon|Midnight)\s*(Daily|[A-Za-z]+(?:-[A-Za-z]+)?)?", part, re.I)
        if not m:
            continue
        a, b = hour_of(m.group(1)), hour_of(m.group(2))
        days = m.group(3) or "Daily"
        if days.lower() == "daily":
            ds = range(7)
        elif "-" in days:
            d0, d1 = (DAY_NAMES.get(x.strip()[:3].title(), DAY_NAMES.get(x.strip())) for x in days.split("-"))
            ds = range(d0, d1 + 1) if d0 is not None and d1 is not None else range(7)
        else:
            d = DAY_NAMES.get(days[:3].title(), DAY_NAMES.get(days))
            ds = [d] if d is not None else range(7)
        for d in ds:
            grid[d, a:b] = True
        parsed = True
    return grid.astype(int).tolist() if parsed else None


def main():
    S = np.load(OUT_DIR / "supply.npz")
    lat, lon = S["latlon"][:, 0], S["latlon"][:, 1]
    b = (lat.min() - 0.005, lat.max() + 0.005, lon.min() - 0.005, lon.max() + 0.005)
    where = f"lat between {b[0]} and {b[1]} and lon between {b[2]} and {b[3]}"
    db = sqlite3.connect(f"file:{LA_PARKING_DB / 'park_smarter_meters.db'}?mode=ro", uri=True)
    lots = []
    for name, lt, ln, spaces, hourly, daily, hours in db.execute(
            f"select lot_name, lat, lon, spaces, hourly_cost, daily_cost, hours from la_city_parking_lots where {where}"):
        lots.append(dict(name=name, operator="LA City", lat=lt, lon=ln, spaces=spaces, rate_per_hr=per_hour(hourly),
                         daily_max=daily_max(daily), hours=hours, open=open_grid(hours)))
    for table, op, cap in (("laz_locations", "LAZ", "total_spaces"), ("spplus_locations", "SP+", "null"),
                           ("ace_locations", "ACE", "null")):
        name_col = "location_name" if table == "ace_locations" else "name"
        for name, lt, ln, spaces in db.execute(f"select {name_col}, lat, lon, {cap} from {table} where {where}"):
            if op == "SP+" and str(name).startswith("(M)"):
                continue  # monthly parkers only
            lots.append(dict(name=name, operator=op, lat=lt, lon=ln, spaces=spaces, rate_per_hr=None, daily_max=None,
                             hours=None, open=None))
    for name, lt, ln, spaces, rate in db.execute(f"select name, lat, lon, space_total, rate from abm_locations where kind='facility' and {where}"):
        lots.append(dict(name=name, operator="ABM", lat=lt, lon=ln, spaces=spaces, rate_per_hr=None, daily_max=None,
                         hours=None, open=None))
    for x in lots:
        x["verified"] = True
    for p in json.load(open(OUT_DIR / "osm_ways.json")).get("parking", []):
        t = p["tags"]
        public = t.get("access") in ("yes", "permissive") or t.get("fee") == "yes"
        likely = "access" not in t and t.get("parking") == "multi-storey"
        if not (public or likely) or t.get("access") in ("private", "customers", "permit", "residents", "no"):
            continue
        cap = t.get("capacity")
        lots.append(dict(name=t.get("name") or ("Unnamed garage" if t.get("parking") == "multi-storey" else "Unnamed lot"),
                         operator=t.get("operator", "OSM"), lat=p["lat"], lon=p["lon"],
                         spaces=int(cap) if cap and cap.isdigit() else None, rate_per_hr=None, daily_max=None,
                         hours=t.get("opening_hours"), open=None, verified=public))
    # merge duplicates within 40 m, preferring entries with a posted rate
    lots.sort(key=lambda x: (x["rate_per_hr"] is None, not x["verified"]))
    pts = xy([x["lat"] for x in lots], [x["lon"] for x in lots])
    keep = []
    for i, p in enumerate(pts):
        if all(np.hypot(*(p - pts[j])) > 40 for j in keep):
            keep.append(i)
    lots = [lots[i] for i in keep]
    json.dump(lots, open(OUT_DIR / "lots.json", "w"), indent=1)
    priced = sum(1 for x in lots if x["rate_per_hr"] is not None)
    print(f"{len(lots)} public lots -> {OUT_DIR / 'lots.json'}; {priced} with posted rates, "
          f"{sum(1 for x in lots if x['open'])} with parsed hours, {sum(1 for x in lots if not x['verified'])} unverified OSM garages")
    for x in lots:
        if x["rate_per_hr"] is not None:
            print(f"  {x['name']}: ${x['rate_per_hr']:.2f}/h, max {x['daily_max']}, {x['hours']}")


if __name__ == "__main__":
    main()
