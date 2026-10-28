#!/usr/bin/env python3
"""Mesoscopic curb-parking simulator: one day, 1-minute steps, block-face supply vs resident/commuter/visitor demand.

Search model: a driver checks block faces in order of distance from the destination (within a walk radius) and
takes the first legal face with a free space. A face that is full or illegal when checked counts as a miss.

Output per block face per 15-minute slot:
  occ        mean parked cars
  p_free     share of minutes the face had a free space that a visitor could legally use
  checks     searchers who looked at the face; hits = how many of them parked there

Usage: python3 scripts/parking_sim/sim.py --weekday 2 --week 2   (Mon=0; week of month 1-5 for sweeping)
Writes data/parking_sim/contention_wd<weekday>_w<week>.npz
"""
import argparse
import heapq
import math
import os
import sys
import time
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demand  # noqa: E402
from anchor import midday_targets  # noqa: E402
from supply import OUT_DIR, SLOTS  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "curb"))
from common import BASE_DIR, CELL_M  # noqa: E402

WALK_R = {"resident": 300.0, "resident_far": 800.0, "commuter": 500.0, "visitor": 400.0}
SEC_PER_CHECK = 25.0          # driving time to look at one more block face
SWEEP_COMPLIANCE = 0.94       # CAL: cars moved before the sweeper; matches citations per event (incl. catch rate)
# Residents who still find nothing after this many 15-minute retries park "elsewhere" (garage, lot, farther, or
# illegally) until their next trip, instead of circling forever and swamping every freed space.
RES_RETRIES = int(os.environ.get("PARKSIM_RES_RETRIES", 1))


class Sim:
    def __init__(self, supply, weekday, week):
        S = supply
        self.cap = np.floor(S["capacity"] + 0.5).astype(int)
        self.xy = S["xy"]
        self.n = len(self.cap)
        sweep = S["sweep_slots"][:, weekday, :] & S["sweep_weeks"][:, week - 1][:, None]
        self.sweep = sweep
        self.res_ok = ~S["nopark"][:, weekday, :] & ~sweep & (self.cap[:, None] > 0)
        # spaces usable in each slot (loading zones take spaces during their hours)
        self.cap_t = np.maximum(self.cap[:, None] - np.rint(S["loading"][:, weekday, :]).astype(int), 0)
        self.vis_ok = self.res_ok & ~S["permit"][:, weekday, :]
        self.occ = np.zeros(self.n, int)
        self.tree = cKDTree(self.xy)
        self._cand = {}
        self.occ_sum = np.zeros((self.n, SLOTS))
        self.free_min = np.zeros((self.n, SLOTS))
        self.checks = np.zeros((self.n, SLOTS))
        self.hits = np.zeros((self.n, SLOTS))
        self.search_log = []  # (kind, slot, parked?, faces checked, walk m)
        self.cited = np.zeros(self.n)
        self.permit_blocked = np.zeros((self.n, SLOTS))
        self.unparked_res = np.zeros(SLOTS)
        self.overflow = []  # (slot, x, y) of residents who gave up on the curb

    def candidates(self, p, r):
        key = (round(p[0], 0), round(p[1], 0), r)
        c = self._cand.get(key)
        if c is None:
            d, idx = self.tree.query(p, k=200, distance_upper_bound=r)
            ok = np.isfinite(d)
            c = (idx[ok], d[ok])
            self._cand[key] = c
        return c

    def park(self, p, kind, slot):
        resident = kind.startswith("resident")
        legal = self.res_ok[:, slot] if resident else self.vis_ok[:, slot]
        idx, dist = self.candidates(p, WALK_R[kind])
        looked = 0
        for f, d in zip(idx, dist):
            if not legal[f]:
                # a non-resident passing a permit-only face with room: where permit tickets would come from
                if not resident and self.res_ok[f, slot] and self.occ[f] < self.cap_t[f, slot]:
                    self.permit_blocked[f, slot] += 1
                continue
            looked += 1
            self.checks[f, slot] += 1
            if self.occ[f] < self.cap_t[f, slot]:
                self.occ[f] += 1
                self.hits[f, slot] += 1
                self.search_log.append((kind, slot, 1, looked, d, p[0], p[1]))
                return f
        self.search_log.append((kind, slot, 0, looked, 0.0, p[0], p[1]))
        return -1

    def park_resident(self, p, slot):
        """Residents widen the search when the blocks near home are full, then keep circling (retry each slot)."""
        f = self.park(p, "resident", slot)
        if f < 0:
            f = self.park(p, "resident_far", slot)
        return f if f >= 0 else -3

    def run(self, D, probes=None):
        """probes: optional test drivers following fixed routes (see evaluate_routes.py); results in self.probe_results."""
        res, com, vis = D["residents"], D["commuters"], D["visitors"]
        probe_q = [(pr["start"], k, 0, 0.0) for k, pr in enumerate(probes or [])]
        heapq.heapify(probe_q)
        self.probe_results = {}
        events = []  # (minute, order, type, payload)
        nres = len(res["xy"])
        car_face = np.full(nres, -1)
        retries = np.zeros(nres, int)
        car_home = res["xy"]
        # residents start the day parked near home, unless away overnight
        for i in RNG.permutation(nres):
            car_face[i] = self.park_resident(car_home[i], 0)
            for a, b in res["away"][i]:
                heapq.heappush(events, (max(1, int(a)), 1, "res_leave", i))
                heapq.heappush(events, (min(1439, int(b)), 2, "res_return", i))
        for j in range(len(com["xy"])):
            heapq.heappush(events, (int(np.clip(com["arrive"][j], 0, 1439)), 3, "com_arrive", j))
        for j in range(len(vis["xy"])):
            heapq.heappush(events, (int(np.clip(vis["arrive"][j], 0, 1439)), 3, "vis_arrive", j))
        departures = []  # heap of (minute, face)
        for minute in range(1440):
            slot = minute // 15
            if minute % 15 == 0:
                self._sweep_start(slot, car_face, car_home)
                for i in np.where(car_face == -3)[0]:  # still circling
                    car_face[i] = self.park_resident(car_home[i], slot)
                    if car_face[i] == -3:
                        retries[i] += 1
                        if retries[i] >= RES_RETRIES:
                            car_face[i] = -4  # parked elsewhere until the next trip
                            self.overflow.append((slot, *car_home[i]))
                self.unparked_res[slot] = (car_face == -3).sum()
            while departures and departures[0][0] <= minute:
                _, f = heapq.heappop(departures)
                self.occ[f] -= 1
            while events and events[0][0] <= minute:
                _, _, typ, i = heapq.heappop(events)
                if typ == "res_leave":
                    if car_face[i] >= 0:
                        self.occ[car_face[i]] -= 1
                    if car_face[i] != -2:
                        car_face[i] = -2  # away
                elif typ == "res_return":
                    if car_face[i] == -2:
                        retries[i] = 0
                        car_face[i] = self.park_resident(car_home[i], slot)
                elif typ == "com_arrive":
                    f = self.park(com["xy"][i], "commuter", slot)
                    if f >= 0:
                        heapq.heappush(departures, (int(min(com["leave"][i], 1439)), f))
                else:
                    f = self.park(vis["xy"][i], "visitor", slot)
                    if f >= 0:
                        heapq.heappush(departures, (int(min(vis["arrive"][i] + vis["dwell"][i], 1439)), f))
            while probe_q and probe_q[0][0] <= minute:
                _, k, idx, elapsed = heapq.heappop(probe_q)
                pr = probes[k]
                if idx >= len(pr["route"]):
                    self.probe_results[k] = (False, elapsed + pr["fallback_s"], elapsed)
                    continue
                parked = None
                for f, walk in pr["faces"][idx]:
                    if self.vis_ok[f, slot] and self.occ[f] < self.cap_t[f, slot]:
                        parked = (f, walk)
                        break
                if parked:
                    f, walk = parked
                    self.occ[f] += 1
                    heapq.heappush(departures, (min(minute + pr["stay_min"], 1439), f))
                    self.probe_results[k] = (True, elapsed + pr["tt"][idx] / 2 + 30 + walk / 1.3, elapsed)
                    continue
                elapsed += pr["tt"][idx] + pr["turn"][idx]
                heapq.heappush(probe_q, (pr["start"] + int(math.ceil(elapsed / 60)), k, idx + 1, elapsed))
            self.occ_sum[:, slot] += self.occ
            self.free_min[:, slot] += self.vis_ok[:, slot] & (self.occ < self.cap_t[:, slot])

    def _sweep_start(self, slot, car_face, car_home):
        """When a sweep window opens, most resident cars on the face move; the rest would be cited."""
        starting = self.sweep[:, slot] & (slot == 0 or ~self.sweep[:, slot - 1])
        for f in np.where(starting & (self.occ > 0))[0]:
            cars = np.where(car_face == f)[0]
            movers = cars[RNG.random(len(cars)) < SWEEP_COMPLIANCE]
            self.cited[f] += len(cars) - len(movers)
            for i in movers:
                self.occ[f] -= 1
                car_face[i] = self.park_resident(self.xy[f], slot)


SEED = int(os.environ.get("PARKSIM_SEED", 1))
RNG = np.random.default_rng(SEED)
NIGHT_SCALE = float(os.environ.get("PARKSIM_NIGHT_SCALE", 1.3))  # CAL: overnight resident stock multiplier (night anchor)
MIDDAY = slice(44, 52)  # 11:00-13:00
ANCHOR_PASSES = 0       # fixed-point refinement diverges: overflow moves cars between cells


def cell_keys(points):
    c = np.floor(points / CELL_M).astype(np.int64)
    return c[:, 0] * 1_000_000 + c[:, 1]


def anchor_multipliers(S, D, exclude_core, mode="midday"):
    """Per-cell resident-stock multipliers from the satellite-trained estimator.

    midday: each cell's simulated midday (reference Wednesday) matches the estimator.
    night:  resident cars are distributed in proportion to the estimator (where cars live), with one global scale
            so the area's midday total matches; the daytime pattern then comes from the simulated dynamics."""
    fk = cell_keys(S["xy"])
    keys = np.unique(fk)
    pos = {k: i for i, k in enumerate(keys)}
    fcell = np.array([pos[k] for k in fk])
    passes = {}
    for name, dd in (("residents", dict(residents=D["residents"], commuters=_empty(), visitors=_empty())),
                     ("others", dict(residents=_empty_res(), commuters=D["commuters"], visitors=D["visitors"]))):
        sim = Sim(S, 2, 2)
        sim.run(dd)
        passes[name] = np.bincount(fcell, weights=(sim.occ_sum[:, MIDDAY] / 15).mean(1), minlength=len(keys))
    R, O = passes["residents"], passes["others"]
    T = midday_targets(keys, exclude_bbox=tuple(S["bbox"]) if exclude_core else None)
    present = R.sum() / max(len(D["residents"]["xy"]), 1)      # share of resident cars home at midday
    if mode == "night":
        hk = [pos[k] for k in cell_keys(D["residents"]["xy"]) if k in pos]
        homes0 = np.bincount(hk, minlength=len(keys)).astype(float)
        k = NIGHT_SCALE * max(T.sum() - O.sum(), 0) / max(present * T.sum(), 1e-9)
        H = k * T                                                  # resident cars that live in each cell
        m = np.where(homes0 > 0.5, np.clip(H / np.maximum(homes0, 1e-9), 0.1, 12.0), 1.0)
        extra = np.where(homes0 <= 0.5, H, 0.0)
    else:
        m = np.where(R > 0.5, np.clip((T - O) / np.maximum(R, 1e-9), 0.25, 8.0), 1.0)
        extra = np.where(R <= 0.5, np.maximum(T - O, 0) / max(present, 0.1), 0.0)
    A = dict(keys=keys, m=m, extra=extra, T=T, R=R, O=O)
    # fixed-point passes: overflow moves cars between cells, so re-simulate and correct toward the target
    for it in range(ANCHOR_PASSES):
        sim = Sim(S, 2, 2)
        sim.run(apply_anchor(D, A, False))
        got = np.bincount(fcell, weights=(sim.occ_sum[:, MIDDAY] / 15).mean(1), minlength=len(keys))
        ratio = np.clip(T / np.maximum(got, 0.5), 0.5, 2.0)
        A["m"] = np.clip(A["m"] * ratio, 0.1, 12.0)
        A["extra"] = A["extra"] * ratio
        ok = T > 1
        print(f"    anchor pass {it + 1}: midday cars vs target, median ratio {np.median(got[ok] / T[ok]):.2f}")
    return A


def apply_anchor(D, A, weekend):
    res = D["residents"]
    pos = {k: i for i, k in enumerate(A["keys"])}
    hk = cell_keys(res["xy"])
    m = np.array([A["m"][pos[k]] if k in pos else 1.0 for k in hk])
    copies = np.floor(m).astype(int) + (RNG.random(len(m)) < (m - np.floor(m)))
    homes = np.repeat(res["xy"], copies, axis=0)
    centers = np.c_[(A["keys"] // 1_000_000 + 0.5) * CELL_M, (A["keys"] % 1_000_000 + 0.5) * CELL_M]
    add = RNG.poisson(A["extra"])
    homes = np.vstack([homes, np.repeat(centers, add, axis=0)])
    res = dict(res, xy=homes, away=demand.resident_trips(len(homes), weekend, res["worker_share"]), onstreet_cars=len(homes))
    return dict(D, residents=res)


def _empty():
    return dict(xy=np.zeros((0, 2)), arrive=np.zeros(0), leave=np.zeros(0), dwell=np.zeros(0))


def _empty_res():
    return dict(xy=np.zeros((0, 2)), away=[])


def prepare_day(S, weekday, anchor="night", anchor_train_all=False, verbose=True):
    """Demand for one simulated day: residents, commuters, visitors (anchored to the satellite estimator)."""
    say = print if verbose else (lambda *a, **k: None)
    t = time.time()
    bbox = tuple(S["bbox"])
    weekend = weekday >= 5
    D = dict(residents=demand.residents(bbox, weekend), commuters=demand.commuters(bbox, weekend),
             visitors=demand.visitors(bbox, weekday))
    say(f"demand: {D['residents']['onstreet_cars']:,} resident cars on street ({D['residents']['units']:,.0f} units),"
        f" {len(D['commuters']['xy']):,} curb-parking commuters, {len(D['visitors']['xy']):,} curb-seeking visitor trips"
        f" from {D['visitors']['places']:,} places  [{time.time() - t:.0f}s]")
    # drop demand that has no modeled curb nearby (outside the street network)
    curb_tree = cKDTree(S["xy"][S["capacity"] >= 0.5])
    for k in ("commuters", "visitors"):
        keep = curb_tree.query(D[k]["xy"])[0] <= 150
        say(f"  {k}: dropped {(~keep).sum():,} of {len(keep):,} with no modeled curb within 150 m")
        D[k] = {kk: (v[keep] if isinstance(v, np.ndarray) and len(v) == len(keep) else v) for kk, v in D[k].items()}
    keep = curb_tree.query(D["residents"]["xy"])[0] <= 150
    say(f"  residents: dropped {(~keep).sum():,} of {len(keep):,} with no modeled curb within 150 m")
    D["residents"]["xy"] = D["residents"]["xy"][keep]
    D["residents"]["away"] = [a for a, k in zip(D["residents"]["away"], keep) if k]
    if anchor != "none":
        tag = (f"cs{demand.ONSTREET_COMMUTE_SHARE}_cc{demand.COMMUTER_CURB}_vs{demand.VISITOR_SCALE}"
               f"_ve{demand.VISITOR_EVENING}_ns{NIGHT_SCALE}")
        cache = OUT_DIR / f"anchor_{anchor}_{'all' if anchor_train_all else 'excl_core'}_{tag}.npz"
        if cache.exists():
            A = dict(np.load(cache))
        else:
            ref = D if weekday == 2 else dict(residents=demand.residents(bbox, False),
                                               commuters=demand.commuters(bbox, False), visitors=demand.visitors(bbox, 2))
            A = anchor_multipliers(S, ref, exclude_core=not anchor_train_all, mode=anchor)
            np.savez(cache, **A)
        D = apply_anchor(D, A, weekend)
        say(f"  anchored: resident cars on street {len(D['residents']['xy']):,}; multiplier median {np.median(A['m']):.2f},"
            f" p10-p90 {np.percentile(A['m'], 10):.2f}-{np.percentile(A['m'], 90):.2f}")
    return D


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekday", type=int, default=2)
    ap.add_argument("--week", type=int, default=2)
    ap.add_argument("--anchor", default=os.environ.get("PARKSIM_ANCHOR", "night"), choices=["night", "midday", "none"],
                    help="how the satellite estimator sets resident stock (none = pure bottom-up)")
    ap.add_argument("--anchor-train-all", action="store_true",
                    help="train the anchor on all cells (production); default leaves the core out for validation")
    args = ap.parse_args()
    t = time.time()
    S = dict(np.load(OUT_DIR / "supply.npz"))
    D = prepare_day(S, args.weekday, args.anchor, args.anchor_train_all)
    sim = Sim(S, args.weekday, args.week)
    sim.run(D)
    occ = sim.occ_sum / 15
    p_free = sim.free_min / 15
    kinds = {"resident": 0, "resident_far": 0, "commuter": 1, "visitor": 2}
    log = np.array([(kinds[k], s, ok, n, d, x, y) for k, s, ok, n, d, x, y in sim.search_log])  # .., destination x/y
    dest = Path(os.environ.get("PARKSIM_OUT", OUT_DIR)) / f"contention_wd{args.weekday}_w{args.week}.npz"
    np.savez_compressed(dest, occ=occ, p_free=p_free, checks=sim.checks, hits=sim.hits, capacity=sim.cap,
                        vis_ok=sim.vis_ok, cited=sim.cited, permit_blocked=sim.permit_blocked,
                        overflow=np.array(sim.overflow).reshape(-1, 3), search_log=log)
    curb = (sim.cap > 0) & S["core"]
    print(f"simulated weekday {args.weekday} week {args.week} in {time.time() - t:.0f}s -> {dest}")
    for hh in (3, 8, 11, 14, 18, 21):
        s = hh * 4
        o = occ[curb, s:s + 4].mean(1).sum() / sim.cap[curb].sum()
        pf = (p_free[curb, s:s + 4].mean(1) * sim.cap[curb]).sum() / sim.cap[curb].sum()
        v = log[(log[:, 1] >= s) & (log[:, 1] < s + 4) & (log[:, 0] == 2)]  # all visitors (core + margin)
        fail = 1 - v[:, 2].mean() if len(v) else float("nan")
        print(f"  {hh:02d}:00  occupancy {o:.0%}  faces with a free legal space {pf:.0%}  visitor searches failing {fail:.0%}")
    print(f"  sweep: {sim.cited.sum():.0f} cars left on swept faces (would be cited)")
    print(f"  residents still circling for a space: 03:00 {sim.unparked_res[12]:.0f}, 21:00 {sim.unparked_res[84]:.0f};"
          f" gave up on the curb today: {len(sim.overflow):,}")


if __name__ == "__main__":
    main()
