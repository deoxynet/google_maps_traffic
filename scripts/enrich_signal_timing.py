#!/usr/bin/env python3
"""
Signal Timing Enrichment Pipeline

Enriches NavigateLA intersection data with estimated signal timing using:
  1. HCM Chapter 19 methodology (volume → flow ratios → Webster cycle → green splits)
  2. Google Maps typical traffic (congestion levels → observed v/c → back-solved green times)

Usage:
  python3 scripts/enrich_signal_timing.py                  # Full pipeline
  python3 scripts/enrich_signal_timing.py --hcm-only       # HCM analysis only (no network)
  python3 scripts/enrich_signal_timing.py --hours 7,8,17   # Specific hours only
  python3 scripts/enrich_signal_timing.py --ids 253,1234   # Specific intersections only
"""

import argparse
import json
import math
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "navigatela.db"
RUST_HELPER = "approach_congestion"

# ── TOD Period Definitions ─────────────────────────────────────────────────

PERIOD_RANGES = {
    "AM":  range(6, 10),    # hours 6-9
    "MID": range(10, 15),   # hours 10-14
    "PM":  range(15, 19),   # hours 15-18
    "OFF": list(range(0, 6)) + list(range(19, 24)),  # hours 0-5, 19-23
}


def hour_to_period(hour: int) -> str:
    """Map an hour (0-23) to its TOD period."""
    for period, hours in PERIOD_RANGES.items():
        if hour in hours:
            return period
    return "OFF"


# ── HCM Parameters ──────────────────────────────────────────────────────────

BASE_SATURATION_FLOW = 1900   # veh/hr/lane (through movements)
LEFT_TURN_SAT_FLOW = 1700    # veh/hr/lane (protected left)
RIGHT_TURN_SAT_FLOW = 1500   # veh/hr/lane (right turn with ped conflicts)
LOST_TIME_PER_PHASE = 4.0    # seconds (yellow + all-red)
PHF = 0.92                   # peak hour factor
MIN_GREEN = 7.0              # seconds
MAX_CYCLE = 180.0            # seconds
MIN_CYCLE = 60.0             # seconds

# Google Maps congestion → v/c mapping
CONGESTION_VC = {
    0: 0.40,  # Normal
    1: 0.75,  # Slow
    2: 0.90,  # Heavy
    3: 1.05,  # Severe
}

# ── Ped Adjustment ──────────────────────────────────────────────────────────

def ped_adjustment(peds_per_hour: int) -> float:
    """HCM Exhibit 19-24 simplified: ped adjustment for conflicting turn movements."""
    if peds_per_hour < 50:
        return 1.0
    elif peds_per_hour < 200:
        return 0.95
    elif peds_per_hour < 400:
        return 0.90
    elif peds_per_hour < 700:
        return 0.85
    else:
        return 0.80


# ── Approach ────────────────────────────────────────────────────────────────

@dataclass
class Approach:
    direction: str  # NB, SB, EB, WB
    left: int = 0
    through: int = 0
    right: int = 0
    total: int = 0
    peds_conflicting: int = 0
    is_oneway: bool = False  # no opposing direction traffic

    @property
    def design_flow(self) -> float:
        return self.total / PHF if self.total > 0 else 0

    def estimate_lanes(self) -> dict:
        """Estimate lane configuration from volumes."""
        lanes = {"left": 0, "through": 0, "right": 0, "shared_lr": False}
        if self.total == 0:
            return lanes

        # Dedicated left turn lane(s)
        if self.left > 300:
            lanes["left"] = 2
        elif self.left > 120:
            lanes["left"] = 1

        # Through lanes
        remaining = self.through
        if lanes["left"] == 0:
            remaining += self.left  # shared left-through

        if remaining > 2800:
            lanes["through"] = 4
        elif remaining > 1800:
            lanes["through"] = 3
        elif remaining > 800:
            lanes["through"] = 2
        elif remaining > 0:
            lanes["through"] = 1

        # Right turn lane
        if self.right > 180:
            lanes["right"] = 1
        elif self.right > 0 and lanes["through"] > 0:
            lanes["shared_lr"] = True

        return lanes

    def saturation_flow(self) -> float:
        """Total approach saturation flow rate (veh/hr)."""
        lanes = self.estimate_lanes()
        ped_adj = ped_adjustment(self.peds_conflicting)

        s = 0.0
        if lanes["left"] > 0:
            s += lanes["left"] * LEFT_TURN_SAT_FLOW
        if lanes["through"] > 0:
            s += lanes["through"] * BASE_SATURATION_FLOW
        if lanes["right"] > 0:
            s += lanes["right"] * RIGHT_TURN_SAT_FLOW * ped_adj
        elif lanes["shared_lr"] and lanes["through"] > 0:
            reduction = (1 - ped_adj) * RIGHT_TURN_SAT_FLOW / lanes["through"]
            s -= reduction

        return max(s, BASE_SATURATION_FLOW)

    def flow_ratio(self) -> float:
        """v/s ratio for this approach."""
        s = self.saturation_flow()
        return self.design_flow / s if s > 0 else 0

    def to_dict(self) -> dict:
        lanes = self.estimate_lanes()
        return {
            "dir": self.direction,
            "left": self.left,
            "thru": self.through,
            "right": self.right,
            "total": self.total,
            "peds": self.peds_conflicting,
            "lanes": lanes["left"] + lanes["through"] + lanes["right"],
            "sat_flow": round(self.saturation_flow()),
            "flow_ratio": round(self.flow_ratio(), 4),
        }


# ── HCM Solver ──────────────────────────────────────────────────────────────

def detect_oneway(approaches: dict) -> set:
    """Detect one-way streets: if opposing direction has 0 volume, it's one-way."""
    oneway_dirs = set()
    opposites = {"NB": "SB", "SB": "NB", "EB": "WB", "WB": "EB"}
    for d, a in approaches.items():
        opp = opposites.get(d)
        if opp and opp not in approaches:
            oneway_dirs.add(d)
            a.is_oneway = True
    return oneway_dirs


def infer_phasing(approaches: dict) -> list:
    """
    Infer signal phasing from approach volumes.
    Returns list of phases, each a dict with 'dirs' (list of direction strings).
    Handles one-way streets, T-intersections, and protected left turns.
    """
    has = {d: d in approaches and approaches[d].total > 0 for d in ["NB", "SB", "EB", "WB"]}
    oneway = detect_oneway(approaches)

    phases = []

    # ── N/S phase(s) ──
    ns_dirs = [d for d in ["NB", "SB"] if has.get(d)]
    if ns_dirs:
        # Check for protected left turns (only if both directions present)
        ns_needs_lt = False
        if "NB" in ns_dirs and "SB" in ns_dirs:
            nb = approaches["NB"]
            sb = approaches["SB"]
            nb_lt_ratio = nb.left / max(nb.through, 1)
            sb_lt_ratio = sb.left / max(sb.through, 1)
            if nb.left > 200 or sb.left > 200 or nb_lt_ratio > 0.15 or sb_lt_ratio > 0.15:
                ns_needs_lt = True

        if ns_needs_lt:
            lt_dirs = []
            if approaches["NB"].left > 50:
                lt_dirs.append("NB_LT")
            if approaches["SB"].left > 50:
                lt_dirs.append("SB_LT")
            if lt_dirs:
                phases.append(lt_dirs)
        phases.append(ns_dirs)

    # ── E/W phase(s) ──
    ew_dirs = [d for d in ["EB", "WB"] if has.get(d)]
    if ew_dirs:
        ew_needs_lt = False
        if "EB" in ew_dirs and "WB" in ew_dirs:
            eb = approaches["EB"]
            wb = approaches["WB"]
            eb_lt_ratio = eb.left / max(eb.through, 1)
            wb_lt_ratio = wb.left / max(wb.through, 1)
            if eb.left > 200 or wb.left > 200 or eb_lt_ratio > 0.15 or wb_lt_ratio > 0.15:
                ew_needs_lt = True

        if ew_needs_lt:
            lt_dirs = []
            if approaches["EB"].left > 50:
                lt_dirs.append("EB_LT")
            if approaches["WB"].left > 50:
                lt_dirs.append("WB_LT")
            if lt_dirs:
                phases.append(lt_dirs)
        phases.append(ew_dirs)

    return phases if phases else [["NB", "SB"], ["EB", "WB"]]


def critical_flow_ratio_for_phase(phase: list, approaches: dict) -> float:
    """Get the critical (maximum) flow ratio for a phase."""
    max_y = 0.0
    for d in phase:
        if d.endswith("_LT"):
            base = d[:-3]
            a = approaches.get(base)
            if a and a.left > 0:
                y = (a.left / PHF) / LEFT_TURN_SAT_FLOW
                max_y = max(max_y, y)
        else:
            a = approaches.get(d)
            if a:
                max_y = max(max_y, a.flow_ratio())
    return max_y


def solve_hcm(approaches: dict, forced_phases: list = None) -> dict:
    """
    Solve for cycle length and green times using Webster's formula.
    Returns dict with phase timings, v/c ratios, feasibility.

    If forced_phases is provided, use that phase structure instead of inferring.
    """
    phases = forced_phases if forced_phases is not None else infer_phasing(approaches)
    n_phases = len(phases)
    total_lost = n_phases * LOST_TIME_PER_PHASE

    critical_ys = [critical_flow_ratio_for_phase(p, approaches) for p in phases]
    Y_c = sum(critical_ys)

    if Y_c >= 1.2:
        return {
            "feasible": False,
            "Y_c": Y_c,
            "num_phases": n_phases,
            "phases": [{"dirs": p} for p in phases],
            "message": f"Over-saturated: Y_c={Y_c:.3f} >= 1.2 (likely data error)",
        }

    # Webster's optimal cycle (or clamp to MAX_CYCLE if over-saturated)
    flags = []
    if Y_c >= 1.0:
        C = MAX_CYCLE
        flags.append(f"clamped_yc_{Y_c:.3f}")
    else:
        C_opt = (1.5 * total_lost + 5) / (1 - Y_c)
        C = max(MIN_CYCLE, min(MAX_CYCLE, C_opt))
    available_green = C - total_lost

    phase_results = []
    for i, phase in enumerate(phases):
        g = (critical_ys[i] / Y_c) * available_green if Y_c > 0 else available_green / n_phases
        g = max(g, MIN_GREEN)

        vc_ratios = {}
        for d in phase:
            if d.endswith("_LT"):
                base = d[:-3]
                a = approaches.get(base)
                if a and a.left > 0:
                    s = LEFT_TURN_SAT_FLOW
                    v = a.left / PHF
                    cap = s * (g / C)
                    vc_ratios[d] = v / cap if cap > 0 else float("inf")
            else:
                a = approaches.get(d)
                if a and a.total > 0:
                    s = a.saturation_flow()
                    v = a.design_flow
                    cap = s * (g / C)
                    vc_ratios[d] = v / cap if cap > 0 else float("inf")

        phase_results.append({
            "dirs": phase,
            "green": round(g, 1),
            "critical_y": round(critical_ys[i], 4),
            "vc": {d: round(v, 3) for d, v in vc_ratios.items()},
        })

    result = {
        "feasible": True,
        "cycle": round(C, 1),
        "num_phases": n_phases,
        "Y_c": round(Y_c, 4),
        "total_lost": total_lost,
        "phases": phase_results,
    }
    if flags:
        result["flags"] = flags
    return result


# ── Google Maps Back-Solve ──────────────────────────────────────────────────

def back_solve_with_gmap(approaches: dict, gmap_congestion: dict, hcm_result: dict) -> dict:
    """
    Back-solve signal timing using Google Maps congestion as observed v/c.

    Given v/c per approach from Google Maps, and known volumes + saturation flows,
    we can solve for green time allocation:
      g_i/C = v_i / (s_i * vc_i)

    And cycle length:
      C = L / (1 - sum(v_i / (s_i * vc_i)))

    Returns dict similar to HCM result, or None if infeasible.
    """
    if not gmap_congestion or not hcm_result.get("feasible"):
        return None

    phases = hcm_result["phases"]
    total_lost = hcm_result["total_lost"]
    flags = []

    # For each phase, compute the green ratio using Google Maps v/c
    green_ratios = []
    for phase in phases:
        max_ratio = 0.0
        for d in phase["dirs"]:
            if d.endswith("_LT"):
                base = d[:-3]
                a = approaches.get(base)
                if not a or a.left == 0:
                    continue
                v = a.left / PHF
                s = LEFT_TURN_SAT_FLOW
            else:
                a = approaches.get(d)
                if not a or a.total == 0:
                    continue
                v = a.design_flow
                s = a.saturation_flow()

            # Get Google Maps v/c for this approach direction
            gmap_dir = d.replace("_LT", "")
            cong_level = gmap_congestion.get(gmap_dir)
            if cong_level is None:
                continue
            vc_observed = CONGESTION_VC.get(cong_level, 0.40)

            # g/C = v / (s * vc)
            ratio = v / (s * vc_observed) if s * vc_observed > 0 else 0
            max_ratio = max(max_ratio, ratio)

        green_ratios.append(max_ratio)

    sum_ratios = sum(green_ratios)
    if sum_ratios <= 0:
        return None

    # C = L / (1 - sum_ratios)
    denom = 1.0 - sum_ratios
    if denom <= 0:
        flags.append("gmap_cycle_infeasible_negative_denom")
        return {"feasible": False, "flags": flags}

    C = total_lost / denom
    if C > 300 or C < 30:
        flags.append(f"gmap_cycle_out_of_range_{C:.0f}s")
        return {"feasible": False, "cycle": round(C, 1), "flags": flags}

    C = max(MIN_CYCLE, min(MAX_CYCLE, C))
    available_green = C - total_lost

    phase_results = []
    for i, phase in enumerate(phases):
        if sum_ratios > 0:
            g = (green_ratios[i] / sum_ratios) * available_green
        else:
            g = available_green / len(phases)
        g = max(g, MIN_GREEN)

        vc_ratios = {}
        for d in phase["dirs"]:
            gmap_dir = d.replace("_LT", "")
            cong_level = gmap_congestion.get(gmap_dir)
            if cong_level is not None:
                vc_ratios[d] = CONGESTION_VC.get(cong_level, 0.40)

        phase_results.append({
            "dirs": phase["dirs"],
            "green": round(g, 1),
            "vc": {d: round(v, 3) for d, v in vc_ratios.items()},
        })

    return {
        "feasible": True,
        "cycle": round(C, 1),
        "phases": phase_results,
        "flags": flags,
    }


# ── Confidence Scoring ──────────────────────────────────────────────────────

def score_confidence(approaches: dict, count_date: str, hcm: dict, gmap: dict) -> tuple:
    """
    Score confidence as high/medium/low with flag reasons.

    Returns (confidence_str, flags_list).
    """
    flags = []

    n_approaches = sum(1 for a in approaches.values() if a.total > 0)
    has_turns = all(
        (a.left + a.through + a.right) > 0
        for a in approaches.values()
        if a.total > 0
    )

    if n_approaches < 4:
        flags.append(f"only_{n_approaches}_approaches")
    if not has_turns:
        flags.append("missing_turn_breakdowns")

    # Date quality
    try:
        year = int(count_date[:4]) if count_date and len(count_date) >= 4 else 0
    except (ValueError, TypeError):
        year = 0
    if year < 2017:
        flags.append(f"old_data_{year}")

    # Feasibility
    if not hcm.get("feasible"):
        flags.append("hcm_infeasible")

    # V/C check
    if hcm.get("feasible"):
        for phase in hcm.get("phases", []):
            for d, vc in phase.get("vc", {}).items():
                if vc > 0.95:
                    flags.append(f"high_vc_{d}_{vc:.2f}")

    # Google Maps comparison
    if gmap and gmap.get("feasible") and hcm.get("feasible"):
        hcm_cycle = hcm["cycle"]
        gmap_cycle = gmap["cycle"]
        diff_pct = abs(hcm_cycle - gmap_cycle) / hcm_cycle if hcm_cycle > 0 else 0
        if diff_pct > 0.20:
            flags.append(f"hcm_gmap_mismatch_{diff_pct:.0%}")
    elif gmap and not gmap.get("feasible"):
        flags.extend(gmap.get("flags", []))

    # Score
    if n_approaches <= 1:
        return "low", flags
    if (not flags and n_approaches >= 4 and has_turns and year >= 2017
            and gmap and gmap.get("feasible")):
        return "high", flags
    elif len(flags) <= 2 and hcm.get("feasible"):
        return "medium", flags
    else:
        return "low", flags


# ── TOD Period Grouping ────────────────────────────────────────────────────

def group_into_periods(results: dict) -> dict:
    """
    Group per-hour HCM results into TOD periods.

    For each (traffic_id, period), pick the design hour (highest total volume)
    and use that hour's HCM result as the period's timing plan.

    Args:
        results: {(traffic_id, hour): row_dict} from Step 2

    Returns:
        {(traffic_id, period): row_dict} with added design_hour and hours fields
    """
    # Group by (tid, period)
    groups = {}  # (tid, period) -> [(hour, row)]
    for (tid, hour), row in results.items():
        period = hour_to_period(hour)
        key = (tid, period)
        if key not in groups:
            groups[key] = []
        groups[key].append((hour, row))

    # Pick design hour per group
    period_results = {}
    for (tid, period), hour_rows in groups.items():
        # Design hour = highest sum of approach totals
        def total_volume(hr_row):
            _, row = hr_row
            return sum(a.total for a in row["approaches"].values())

        hour_rows.sort(key=total_volume, reverse=True)
        design_hour, design_row = hour_rows[0]
        all_hours = sorted(h for h, _ in hour_rows)

        period_row = dict(design_row)
        period_row["period"] = period
        period_row["design_hour"] = design_hour
        period_row["hours"] = all_hours
        # Remove the per-hour "hour" key
        period_row.pop("hour", None)

        period_results[(tid, period)] = period_row

    return period_results


def unify_phase_structure(results: dict) -> int:
    """
    Ensure each intersection uses the same phase structure across all TOD periods.

    Real signals don't change their phase count between TOD plans. When AM and PM
    have different num_phases (e.g., left-turn protection threshold crossed in only
    one period), adopt the phase structure from the highest-volume period and re-solve
    the others with that forced structure.

    Mutates results in-place. Returns count of re-solved periods.
    """
    # Group by traffic_id
    by_tid = {}  # tid -> [(period, key)]
    for (tid, period) in results:
        if tid not in by_tid:
            by_tid[tid] = []
        by_tid[tid].append(period)

    re_solved = 0
    for tid, periods in by_tid.items():
        if len(periods) < 2:
            continue

        # Check if phase counts differ
        phase_counts = {}
        for period in periods:
            key = (tid, period)
            hcm = results[key]["hcm"]
            if hcm.get("feasible"):
                phase_counts[period] = hcm.get("num_phases", 0)

        if len(set(phase_counts.values())) <= 1:
            continue  # all same, nothing to unify

        # Pick the design period: highest total volume
        def total_vol(period):
            approaches = results[(tid, period)]["approaches"]
            return sum(a.total for a in approaches.values())

        design_period = max(phase_counts.keys(), key=total_vol)
        design_hcm = results[(tid, design_period)]["hcm"]
        design_phases = [p["dirs"] for p in design_hcm["phases"]]

        # Re-solve other periods with the design period's phase structure
        for period in phase_counts:
            if period == design_period:
                continue
            if phase_counts[period] == phase_counts[design_period]:
                continue

            key = (tid, period)
            approaches = results[key]["approaches"]
            new_hcm = solve_hcm(approaches, forced_phases=design_phases)

            # Carry over the unified flag
            if "flags" not in new_hcm:
                new_hcm["flags"] = []
            new_hcm["flags"].append(f"unified_phases_from_{design_period}")

            results[key]["hcm"] = new_hcm
            re_solved += 1

    return re_solved


# ── Database Queries ────────────────────────────────────────────────────────

def get_best_counts(conn: sqlite3.Connection, traffic_ids: list = None) -> dict:
    """
    Select the best (most recent, most complete) count date per traffic_id.

    Returns: {traffic_id: best_date}
    """
    id_filter = ""
    params = []
    if traffic_ids:
        placeholders = ",".join("?" * len(traffic_ids))
        id_filter = f"AND m.traffic_id IN ({placeholders})"
        params = list(traffic_ids)

    rows = conn.execute(f"""
        WITH date_quality AS (
            SELECT
                m.traffic_id,
                m.date,
                COUNT(DISTINCT m.approach) as n_approaches,
                SUM(CASE WHEN m.left_turn + m.through + m.right_turn > 0
                         AND m.total > 0 THEN 1 ELSE 0 END) as n_with_turns
            FROM manual_counts m
            WHERE m.date IS NOT NULL AND m.date != ''
              AND m.approach IN ('NB', 'SB', 'EB', 'WB')
              AND m.total > 0
              {id_filter}
            GROUP BY m.traffic_id, m.date
        )
        SELECT traffic_id, date,
               n_approaches, n_with_turns
        FROM date_quality
        WHERE n_approaches >= 2
        ORDER BY traffic_id, n_with_turns DESC, n_approaches DESC, date DESC
    """, params).fetchall()

    best = {}
    for r in rows:
        tid = r[0]
        if tid not in best:
            best[tid] = r[1]
    return best


def bulk_load_all_data(conn: sqlite3.Connection, best_dates: dict, filter_hours: list = None):
    """
    Bulk-load all manual counts, ped counts, and intersection info in 3 queries.

    Returns:
        counts: {(traffic_id, hour): {direction: Approach}}
        ped_data: {(traffic_id, hour): {crossing: count}}
        ix_info: {traffic_id: {name, lat, lon}}
        hours_per_tid: {traffic_id: [hours]}
        day_names: {traffic_id: day_name}
    """
    # Build (traffic_id, date) pairs for the WHERE clause
    tids = list(best_dates.keys())
    if not tids:
        return {}, {}, {}, {}, {}

    # 1. Bulk load all intersection info
    placeholders = ",".join("?" * len(tids))
    ix_rows = conn.execute(f"""
        SELECT traffic_id, intersection, lat, lon
        FROM intersections
        WHERE traffic_id IN ({placeholders})
    """, tids).fetchall()
    ix_info = {}
    for r in ix_rows:
        ix_info[r[0]] = {"name": r[1], "lat": r[2], "lon": r[3]}

    # 2. Bulk load all manual counts for best dates
    # Build a temp table or use a big OR — temp table approach is cleaner
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _best_dates (tid INTEGER, dt TEXT)")
    conn.execute("DELETE FROM _best_dates")
    conn.executemany(
        "INSERT INTO _best_dates VALUES (?, ?)",
        [(tid, date) for tid, date in best_dates.items()],
    )

    hour_filter = ""
    hour_params = []
    if filter_hours:
        hour_placeholders = ",".join("?" * len(filter_hours))
        hour_filter = f"AND m.hour_start IN ({hour_placeholders})"
        hour_params = list(filter_hours)

    count_rows = conn.execute(f"""
        SELECT m.traffic_id, m.hour_start, m.approach,
               m.left_turn, m.through, m.right_turn, m.total,
               m.day_of_week, m.pdf_id,
               ABS(COALESCE(m.left_turn,0) + COALESCE(m.through,0)
                   + COALESCE(m.right_turn,0) - m.total) as turn_diff
        FROM manual_counts m
        JOIN _best_dates bd ON bd.tid = m.traffic_id AND bd.dt = m.date
        WHERE m.approach IN ('NB', 'SB', 'EB', 'WB')
          AND m.total > 0
          {hour_filter}
        ORDER BY m.traffic_id, m.hour_start, m.approach, turn_diff, m.pdf_id DESC
    """, hour_params).fetchall()

    # Process into per-(tid, hour) approach dicts
    counts = {}       # (tid, hour) -> {dir: Approach}
    hours_per_tid = {}  # tid -> set of hours
    day_names = {}    # tid -> day name

    for r in count_rows:
        tid, hour, direction = r[0], r[1], r[2]
        key = (tid, hour)

        if key not in counts:
            counts[key] = {}
        if direction in counts[key]:
            continue  # keep first (best quality) per approach

        counts[key][direction] = Approach(
            direction=direction,
            left=r[3] or 0,
            through=r[4] or 0,
            right=r[5] or 0,
            total=r[6] or 0,
        )

        if tid not in hours_per_tid:
            hours_per_tid[tid] = set()
        hours_per_tid[tid].add(hour)

        if tid not in day_names and r[7]:
            day_names[tid] = normalize_day_name(r[7])

    # 3. Bulk load all ped counts
    ped_rows = conn.execute(f"""
        SELECT p.traffic_id, p.hour_start, p.crossing,
               MAX(p.pedestrians) as peds
        FROM manual_ped_counts p
        WHERE p.traffic_id IN ({placeholders})
          {hour_filter.replace('m.hour_start', 'p.hour_start')}
        GROUP BY p.traffic_id, p.hour_start, p.crossing
    """, tids + hour_params).fetchall()

    ped_data = {}  # (tid, hour) -> {crossing: count}
    for r in ped_rows:
        key = (r[0], r[1])
        if key not in ped_data:
            ped_data[key] = {}
        ped_data[key][r[2]] = r[3]

    conn.execute("DROP TABLE IF EXISTS _best_dates")
    conn.commit()

    return counts, ped_data, ix_info, hours_per_tid, day_names


def apply_ped_conflicts(approaches: dict, ped_data: dict):
    """Assign conflicting ped volumes to approaches."""
    conflict_map = {"NB": "E/L", "SB": "W/L", "EB": "N/L", "WB": "S/L"}
    for d, a in approaches.items():
        crossing = conflict_map.get(d, "")
        a.peds_conflicting = ped_data.get(crossing, 0)


def create_schema(conn: sqlite3.Connection):
    """Create the signal_timing table if it doesn't exist."""
    conn.execute("DROP TABLE IF EXISTS signal_timing")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_timing (
            traffic_id INTEGER NOT NULL,
            period TEXT NOT NULL,
            design_hour INTEGER,
            hours TEXT,
            count_date TEXT,
            approaches TEXT,
            num_phases INTEGER,
            y_critical REAL,
            hcm_cycle REAL,
            hcm_phases TEXT,
            gmap_congestion TEXT,
            gmap_cycle REAL,
            gmap_phases TEXT,
            confidence TEXT,
            flags TEXT,
            PRIMARY KEY (traffic_id, period)
        )
    """)
    conn.commit()


# ── Google Maps Helper ──────────────────────────────────────────────────────

def fetch_gmap_congestion(intersections: list, day: str, hour: int) -> dict:
    """
    Call the Rust approach_congestion helper to get Google Maps congestion.

    Args:
        intersections: list of {"id": int, "lat": float, "lon": float}
        day: e.g. "Tuesday"
        hour: 0-23

    Returns: {traffic_id: {"NB": 0, "SB": 2, ...}}
    """
    input_data = {
        "day": day,
        "hour": hour,
        "intersections": intersections,
    }

    try:
        result = subprocess.run(
            ["cargo", "run", "--release", "--example", RUST_HELPER],
            input=json.dumps(input_data),
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(BASE_DIR),
        )
    except subprocess.TimeoutExpired:
        print("  WARNING: Rust helper timed out", file=sys.stderr)
        return {}
    except FileNotFoundError:
        print("  WARNING: cargo not found, skipping Google Maps enrichment", file=sys.stderr)
        return {}

    if result.returncode != 0:
        print(f"  WARNING: Rust helper failed: {result.stderr[:500]}", file=sys.stderr)
        return {}

    if result.stderr:
        # Rust helper prints progress to stderr
        for line in result.stderr.strip().split("\n"):
            print(f"  [rust] {line}", file=sys.stderr)

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  WARNING: couldn't parse Rust helper output", file=sys.stderr)
        return {}

    return {item["id"]: item["congestion"] for item in output}


# ── Day of Week Helpers ─────────────────────────────────────────────────────

DAY_NAMES = {
    "monday": "Monday", "tuesday": "Tuesday", "wednesday": "Wednesday",
    "thursday": "Thursday", "friday": "Friday", "saturday": "Saturday",
    "sunday": "Sunday",
    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
    "thu": "Thursday", "fri": "Friday", "sat": "Saturday", "sun": "Sunday",
}


def normalize_day_name(raw: str) -> str:
    """Normalize day name for the Rust helper."""
    if not raw:
        return "Tuesday"  # default to typical weekday
    return DAY_NAMES.get(raw.lower().strip(), "Tuesday")


def get_count_day(conn: sqlite3.Connection, traffic_id: int, date: str) -> str:
    """Get the day of week from the manual count data."""
    row = conn.execute(
        "SELECT day_of_week FROM manual_counts WHERE traffic_id = ? AND date = ? LIMIT 1",
        (traffic_id, date),
    ).fetchone()
    if row and row[0]:
        return normalize_day_name(row[0])
    return "Tuesday"


# ── Main Pipeline ───────────────────────────────────────────────────────────

def run_pipeline(args):
    conn = sqlite3.connect(str(DB_PATH))
    create_schema(conn)

    # Parse filter args
    filter_ids = None
    if args.ids:
        filter_ids = [int(x.strip()) for x in args.ids.split(",")]

    filter_hours = None
    if args.hours:
        filter_hours = [int(x.strip()) for x in args.hours.split(",")]

    # Step 1: Select best count per intersection
    print("Step 1: Selecting best count date per intersection...")
    best_dates = get_best_counts(conn, filter_ids)
    print(f"  Found {len(best_dates)} intersections with usable manual counts")

    if not best_dates:
        print("  No data found. Exiting.")
        return

    # Bulk load all data in 3 queries
    print("  Loading all counts, peds, and intersection info...")
    all_counts, all_peds, ix_info, hours_per_tid, day_names = bulk_load_all_data(
        conn, best_dates, filter_hours
    )
    print(f"  Loaded {len(all_counts)} (intersection, hour) count sets")

    # Step 2: HCM analysis for each intersection × hour, then group into TOD periods
    print("\nStep 2: Running HCM analysis...")
    hourly_results = {}  # (traffic_id, hour) -> row dict

    for key, approaches in all_counts.items():
        tid, hour = key
        if not approaches:
            continue

        ped_data = all_peds.get(key, {})
        apply_ped_conflicts(approaches, ped_data)

        hcm = solve_hcm(approaches)
        info = ix_info.get(tid, {"name": "?", "lat": 0, "lon": 0})

        hourly_results[key] = {
            "traffic_id": tid,
            "hour": hour,
            "count_date": best_dates[tid],
            "name": info["name"],
            "lat": info["lat"],
            "lon": info["lon"],
            "approaches": approaches,
            "hcm": hcm,
            "gmap_congestion": None,
            "gmap_result": None,
            "day_name": day_names.get(tid, "Tuesday"),
        }

    print(f"  Generated {len(hourly_results)} intersection×hour HCM results")

    feasible = sum(1 for r in hourly_results.values() if r["hcm"].get("feasible"))
    print(f"  Feasible: {feasible}, Infeasible: {len(hourly_results) - feasible}")

    # Group into TOD periods
    print("  Grouping into TOD periods...")
    results = group_into_periods(hourly_results)

    # Period summary
    period_counts = {}
    for (tid, period) in results:
        period_counts[period] = period_counts.get(period, 0) + 1
    period_summary = ", ".join(f"{p}={c}" for p, c in sorted(period_counts.items()))
    print(f"  {len(results)} period rows ({period_summary})")

    # Unify phase structure across periods per intersection
    unified = unify_phase_structure(results)
    if unified:
        print(f"  Unified phase structure for {unified} period(s)")

    # Step 3: Google Maps enrichment (design hours only)
    if not args.hcm_only:
        print("\nStep 3: Fetching Google Maps typical traffic (design hours only)...")

        # Group by (day, design_hour) for batch fetching
        batches = {}  # (day, hour) -> [(tid, lat, lon)]
        for (tid, period), r in results.items():
            day = r["day_name"]
            design_hour = r["design_hour"]
            batch_key = (day, design_hour)
            if batch_key not in batches:
                batches[batch_key] = []
            info = ix_info[tid]
            if info["lat"] and info["lon"]:
                batches[batch_key].append({
                    "id": tid,
                    "lat": info["lat"],
                    "lon": info["lon"],
                })

        print(f"  {len(batches)} (day, hour) batches to fetch")

        # Build reverse lookup: (tid, design_hour) -> [(tid, period)]
        tid_hour_to_period = {}
        for (tid, period), r in results.items():
            tid_hour_to_period[(tid, r["design_hour"])] = (tid, period)

        for (day, hour), ixs in sorted(batches.items()):
            print(f"  Fetching {day} {hour:02d}:00 ({len(ixs)} intersections)...")
            gmap = fetch_gmap_congestion(ixs, day, hour)

            for ix in ixs:
                tid = ix["id"]
                period_key = tid_hour_to_period.get((tid, hour))
                if period_key and period_key in results and tid in gmap:
                    results[period_key]["gmap_congestion"] = gmap[tid]

                    # Back-solve
                    gmap_result = back_solve_with_gmap(
                        results[period_key]["approaches"],
                        gmap[tid],
                        results[period_key]["hcm"],
                    )
                    results[period_key]["gmap_result"] = gmap_result

            matched = sum(1 for ix in ixs if ix["id"] in gmap)
            print(f"    Matched: {matched}/{len(ixs)}")
    else:
        print("\nStep 3: Skipped (--hcm-only)")

    # Step 4: Confidence scoring and DB write
    print("\nStep 4: Scoring confidence and writing to database...")

    # Clear existing results for these intersections
    if filter_ids:
        placeholders = ",".join("?" * len(filter_ids))
        conn.execute(f"DELETE FROM signal_timing WHERE traffic_id IN ({placeholders})", filter_ids)
    else:
        conn.execute("DELETE FROM signal_timing")

    rows_written = 0
    confidence_counts = {"high": 0, "medium": 0, "low": 0}

    for key, r in sorted(results.items()):
        tid, period = key
        approaches = r["approaches"]
        hcm = r["hcm"]
        gmap_cong = r["gmap_congestion"]
        gmap_result = r["gmap_result"]

        confidence, flags = score_confidence(approaches, r["count_date"], hcm, gmap_result)
        confidence_counts[confidence] += 1

        # Serialize
        approaches_json = json.dumps([a.to_dict() for a in approaches.values()])
        hcm_phases_json = json.dumps(hcm.get("phases")) if hcm.get("phases") else None
        gmap_cong_json = json.dumps(gmap_cong) if gmap_cong else None
        gmap_phases_json = json.dumps(gmap_result.get("phases")) if gmap_result and gmap_result.get("phases") else None
        flags_json = json.dumps(flags) if flags else None
        hours_json = json.dumps(r["hours"])

        conn.execute("""
            INSERT OR REPLACE INTO signal_timing
            (traffic_id, period, design_hour, hours, count_date, approaches,
             num_phases, y_critical, hcm_cycle, hcm_phases,
             gmap_congestion, gmap_cycle, gmap_phases,
             confidence, flags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            tid,
            period,
            r["design_hour"],
            hours_json,
            r["count_date"],
            approaches_json,
            hcm.get("num_phases"),
            hcm.get("Y_c"),
            hcm.get("cycle") if hcm.get("feasible") else None,
            hcm_phases_json,
            gmap_cong_json,
            gmap_result.get("cycle") if gmap_result and gmap_result.get("feasible") else None,
            gmap_phases_json,
            confidence,
            flags_json,
        ))
        rows_written += 1

    conn.commit()
    conn.close()

    # Summary
    print(f"\n{'=' * 70}")
    print(f"SIGNAL TIMING ENRICHMENT COMPLETE")
    print(f"{'=' * 70}")
    print(f"  Intersections processed: {len(best_dates)}")
    print(f"  TOD periods: {rows_written} rows ({period_summary})")
    print(f"  Confidence: high={confidence_counts['high']}, "
          f"medium={confidence_counts['medium']}, low={confidence_counts['low']}")
    if not args.hcm_only:
        gmap_matched = sum(1 for r in results.values() if r["gmap_congestion"])
        print(f"  Google Maps matched: {gmap_matched}/{len(results)}")
    print(f"\nVerify:")
    print(f"  sqlite3 {DB_PATH} \"SELECT count(*), period FROM signal_timing GROUP BY period\"")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Signal Timing Enrichment Pipeline")
    parser.add_argument("--hcm-only", action="store_true",
                        help="Run HCM analysis only, skip Google Maps enrichment")
    parser.add_argument("--hours", type=str, default=None,
                        help="Comma-separated hours to process (e.g. 7,8,17)")
    parser.add_argument("--ids", type=str, default=None,
                        help="Comma-separated traffic_ids to process")
    args = parser.parse_args()
    run_pipeline(args)


if __name__ == "__main__":
    main()
