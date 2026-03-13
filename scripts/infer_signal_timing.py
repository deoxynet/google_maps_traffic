#!/usr/bin/env python3
"""
Infer traffic signal timing from NavigateLA turn-movement counts
using HCM Chapter 19 (Signalized Intersections) methodology.

Core approach:
  - Saturation flow rate s = 1900 veh/hr/lane (HCM base), adjusted for turns/peds
  - Flow ratio y = v / s  for each approach
  - Critical flow ratio Y_c = sum of critical y per phase
  - Webster optimal cycle: C = (1.5L + 5) / (1 - Y_c)
  - Green allocation: g_i = (y_i / Y_c) * (C - L)

Lane counts are estimated from volumes (refinable with OSM data).
"""

import sqlite3
import math
from dataclasses import dataclass, field

DB_PATH = "data/navigatela.db"

# ── HCM parameters ──────────────────────────────────────────────────────────

BASE_SATURATION_FLOW = 1900  # veh/hr/lane for through movements
LEFT_TURN_SAT_FLOW = 1700   # protected left
RIGHT_TURN_SAT_FLOW = 1500  # right turn with ped conflicts
LOST_TIME_PER_PHASE = 4.0   # seconds (yellow + all-red)
PHF = 0.92                  # peak hour factor (typical urban)
MIN_GREEN = 7.0             # minimum green time (seconds)
MAX_CYCLE = 180.0           # max practical cycle length
MIN_CYCLE = 60.0            # min practical cycle length

# Ped adjustment factors (HCM Exhibit 19-24 simplified)
# Reduces saturation flow on conflicting turn movements
def ped_adjustment(peds_per_hour: int) -> float:
    """Ped adjustment factor for right turns (fRpb) and left turns (fLpb)."""
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


@dataclass
class Approach:
    direction: str  # NB, SB, EB, WB
    left: int = 0
    through: int = 0
    right: int = 0
    total: int = 0
    peds_conflicting: int = 0  # peds on the crossing that conflicts with right turns

    @property
    def design_flow(self) -> float:
        return self.total / PHF if self.total > 0 else 0

    def estimate_lanes(self) -> dict:
        """Estimate lane configuration from volumes."""
        lanes = {"left": 0, "through": 0, "right": 0, "shared_lr": False}

        if self.total == 0:
            return lanes

        # Dedicated left turn lane if left volume is significant
        if self.left > 120:
            lanes["left"] = 1
        elif self.left > 250:
            lanes["left"] = 2

        # Through lanes based on through + shared volume
        remaining = self.through
        if lanes["left"] == 0:
            remaining += self.left  # shared left-through lane

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
            lanes["shared_lr"] = True  # shared through-right

        return lanes

    def saturation_flow(self) -> float:
        """Estimate total approach saturation flow rate (veh/hr)."""
        lanes = self.estimate_lanes()
        ped_adj = ped_adjustment(self.peds_conflicting)

        s = 0.0
        # Left turn lanes
        if lanes["left"] > 0:
            s += lanes["left"] * LEFT_TURN_SAT_FLOW
        # Through lanes
        if lanes["through"] > 0:
            s += lanes["through"] * BASE_SATURATION_FLOW
        # Right turn lane (adjusted for peds)
        if lanes["right"] > 0:
            s += lanes["right"] * RIGHT_TURN_SAT_FLOW * ped_adj
        elif lanes["shared_lr"]:
            # Shared through-right: already counted in through, but
            # apply ped reduction to rightmost through lane
            if lanes["through"] > 0:
                reduction = (1 - ped_adj) * RIGHT_TURN_SAT_FLOW / lanes["through"]
                s -= reduction

        # Minimum: at least one lane at base saturation
        return max(s, BASE_SATURATION_FLOW)

    def flow_ratio(self) -> float:
        """v/s ratio for this approach."""
        s = self.saturation_flow()
        return self.design_flow / s if s > 0 else 0


@dataclass
class Intersection:
    traffic_id: int
    name: str
    lat: float
    lon: float
    approaches: dict = field(default_factory=dict)  # direction -> Approach

    def infer_phasing(self) -> list:
        """
        Infer signal phasing from approach volumes.

        Returns list of phases, each a list of approach directions
        that move concurrently.
        """
        has_nb = self.approaches.get("NB", Approach("NB")).total > 0
        has_sb = self.approaches.get("SB", Approach("SB")).total > 0
        has_eb = self.approaches.get("EB", Approach("EB")).total > 0
        has_wb = self.approaches.get("WB", Approach("WB")).total > 0

        nb = self.approaches.get("NB", Approach("NB"))
        sb = self.approaches.get("SB", Approach("SB"))
        eb = self.approaches.get("EB", Approach("EB"))
        wb = self.approaches.get("WB", Approach("WB"))

        phases = []

        # N/S phase
        ns_dirs = []
        if has_nb:
            ns_dirs.append("NB")
        if has_sb:
            ns_dirs.append("SB")

        # Check if N/S needs protected left turn phases
        ns_needs_lt = False
        if has_nb and has_sb:
            # Protected left if left volume > 200 or left/through > 0.15
            nb_lt_ratio = nb.left / max(nb.through, 1)
            sb_lt_ratio = sb.left / max(sb.through, 1)
            if nb.left > 200 or sb.left > 200 or nb_lt_ratio > 0.15 or sb_lt_ratio > 0.15:
                ns_needs_lt = True

        if ns_needs_lt:
            # Leading left turn phase, then N/S through
            lt_dirs = []
            if nb.left > 50:
                lt_dirs.append("NB_LT")
            if sb.left > 50:
                lt_dirs.append("SB_LT")
            if lt_dirs:
                phases.append(lt_dirs)
        if ns_dirs:
            phases.append(ns_dirs)

        # E/W phase
        ew_dirs = []
        if has_eb:
            ew_dirs.append("EB")
        if has_wb:
            ew_dirs.append("WB")

        ew_needs_lt = False
        if has_eb and has_wb:
            eb_lt_ratio = eb.left / max(eb.through, 1)
            wb_lt_ratio = wb.left / max(wb.through, 1)
            if eb.left > 200 or wb.left > 200 or eb_lt_ratio > 0.15 or wb_lt_ratio > 0.15:
                ew_needs_lt = True

        if ew_needs_lt:
            lt_dirs = []
            if eb.left > 50:
                lt_dirs.append("EB_LT")
            if wb.left > 50:
                lt_dirs.append("WB_LT")
            if lt_dirs:
                phases.append(lt_dirs)
        if ew_dirs:
            phases.append(ew_dirs)

        return phases if phases else [["NB", "SB"], ["EB", "WB"]]

    def critical_flow_ratio_for_phase(self, phase: list) -> float:
        """Get the critical (maximum) flow ratio for a phase."""
        max_y = 0.0
        for direction in phase:
            if direction.endswith("_LT"):
                base_dir = direction[:-3]
                appr = self.approaches.get(base_dir, Approach(base_dir))
                # Left turn only: flow ratio based on left volume vs LT sat flow
                if appr.left > 0:
                    lanes = 1
                    y = (appr.left / PHF) / (lanes * LEFT_TURN_SAT_FLOW)
                    max_y = max(max_y, y)
            else:
                appr = self.approaches.get(direction, Approach(direction))
                y = appr.flow_ratio()
                max_y = max(max_y, y)
        return max_y

    def solve_timing(self) -> dict:
        """
        Solve for cycle length and green times using Webster's formula.

        Returns dict with cycle_length, phases with green times, and v/c ratios.
        """
        phases = self.infer_phasing()
        n_phases = len(phases)
        total_lost = n_phases * LOST_TIME_PER_PHASE

        # Critical flow ratios
        critical_ys = []
        for phase in phases:
            y_c = self.critical_flow_ratio_for_phase(phase)
            critical_ys.append(y_c)

        Y_c = sum(critical_ys)

        # Check if intersection is over-saturated
        if Y_c >= 1.0:
            # Can't solve — demand exceeds capacity at any cycle length
            return {
                "feasible": False,
                "Y_c": Y_c,
                "phases": phases,
                "message": f"Over-saturated: Y_c={Y_c:.3f} >= 1.0",
            }

        # Webster's optimal cycle length
        C_opt = (1.5 * total_lost + 5) / (1 - Y_c)
        C = max(MIN_CYCLE, min(MAX_CYCLE, C_opt))

        # Available green time
        available_green = C - total_lost

        # Allocate green proportionally to critical flow ratios
        phase_results = []
        for i, phase in enumerate(phases):
            if Y_c > 0:
                g = (critical_ys[i] / Y_c) * available_green
            else:
                g = available_green / n_phases
            g = max(g, MIN_GREEN)

            # v/c ratio for this phase
            vc_ratios = {}
            for direction in phase:
                if direction.endswith("_LT"):
                    base_dir = direction[:-3]
                    appr = self.approaches.get(base_dir, Approach(base_dir))
                    if appr.left > 0:
                        s = LEFT_TURN_SAT_FLOW
                        v = appr.left / PHF
                        capacity = s * (g / C)
                        vc = v / capacity if capacity > 0 else float("inf")
                        vc_ratios[direction] = vc
                else:
                    appr = self.approaches.get(direction, Approach(direction))
                    if appr.total > 0:
                        s = appr.saturation_flow()
                        v = appr.design_flow
                        capacity = s * (g / C)
                        vc = v / capacity if capacity > 0 else float("inf")
                        vc_ratios[direction] = vc

            phase_results.append({
                "directions": phase,
                "green": g,
                "critical_y": critical_ys[i],
                "vc_ratios": vc_ratios,
            })

        return {
            "feasible": True,
            "cycle_length": C,
            "cycle_webster": C_opt,
            "total_lost": total_lost,
            "Y_c": Y_c,
            "phases": phase_results,
        }


def load_corridor(db_path: str, hour: int = 17) -> list:
    """Load Figueroa corridor intersections from database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Get most recent manual counts for DTLA Figueroa, PM peak
    rows = conn.execute("""
        WITH ranked AS (
          SELECT m.traffic_id, i.intersection, i.lat, i.lon,
                 m.date, m.hour_start, m.approach,
                 m.left_turn, m.through, m.right_turn, m.total,
                 ROW_NUMBER() OVER (PARTITION BY m.traffic_id, m.approach
                                    ORDER BY m.date DESC) as rn
          FROM manual_counts m
          JOIN intersections i ON i.traffic_id = m.traffic_id
          WHERE i.intersection LIKE '%FIGUEROA%'
            AND i.lat BETWEEN 34.04 AND 34.063
            AND m.hour_start = ?
            AND m.approach IN ('NB','SB','EB','WB')
            AND m.date IS NOT NULL AND m.date != ''
            AND m.total > 0
        )
        SELECT * FROM ranked WHERE rn = 1
        ORDER BY lat DESC, approach
    """, (hour,)).fetchall()

    # Get ped counts
    ped_rows = conn.execute("""
        SELECT p.traffic_id, p.hour_start, p.crossing,
               MAX(p.pedestrians) as pedestrians
        FROM manual_ped_counts p
        JOIN intersections i ON i.traffic_id = p.traffic_id
        WHERE i.intersection LIKE '%FIGUEROA%'
          AND i.lat BETWEEN 34.04 AND 34.063
          AND p.hour_start = ?
        GROUP BY p.traffic_id, p.crossing
    """, (hour,)).fetchall()

    conn.close()

    # Build ped lookup: traffic_id -> {crossing -> peds}
    ped_data = {}
    for r in ped_rows:
        tid = r["traffic_id"]
        if tid not in ped_data:
            ped_data[tid] = {}
        ped_data[tid][r["crossing"]] = r["pedestrians"]

    # Build intersections
    intersections = {}
    for r in rows:
        tid = r["traffic_id"]
        if tid not in intersections:
            intersections[tid] = Intersection(
                traffic_id=tid,
                name=r["intersection"],
                lat=r["lat"],
                lon=r["lon"],
            )
        direction = r["approach"]
        appr = Approach(
            direction=direction,
            left=r["left_turn"] or 0,
            through=r["through"] or 0,
            right=r["right_turn"] or 0,
            total=r["total"] or 0,
        )

        # Assign conflicting ped volumes
        # NB right turn conflicts with E/L crossing (east leg)
        # SB right turn conflicts with W/L crossing
        # EB right turn conflicts with N/L crossing
        # WB right turn conflicts with S/L crossing
        peds = ped_data.get(tid, {})
        conflict_map = {"NB": "E/L", "SB": "W/L", "EB": "S/L", "WB": "N/L"}
        crossing = conflict_map.get(direction, "")
        appr.peds_conflicting = peds.get(crossing, 0)

        intersections[tid].approaches[direction] = appr

    # Sort by latitude (north to south along corridor)
    return sorted(intersections.values(), key=lambda x: -x.lat)


def main():
    corridor = load_corridor(DB_PATH, hour=17)

    print("=" * 100)
    print("HCM SIGNAL TIMING INFERENCE — FIGUEROA ST CORRIDOR (DTLA)")
    print("PM Peak Hour (5-6 PM) | Methodology: HCM Ch.19 + Webster's Optimal Cycle")
    print("=" * 100)

    for ix in corridor:
        print(f"\n{'─' * 100}")
        print(f"  {ix.name}  (id={ix.traffic_id}, {ix.lat:.5f}, {ix.lon:.5f})")
        print(f"{'─' * 100}")

        # Print approach volumes
        print(f"  {'Approach':<8} {'Left':>6} {'Thru':>6} {'Right':>6} {'Total':>6}"
              f"  {'Lanes (est)':>14}  {'Sat Flow':>8}  {'v/s':>6}  {'Peds':>5}")
        for d in ["NB", "SB", "EB", "WB"]:
            a = ix.approaches.get(d)
            if a and a.total > 0:
                lanes = a.estimate_lanes()
                lane_str = f"L:{lanes['left']} T:{lanes['through']} R:{lanes['right']}"
                print(f"  {d:<8} {a.left:>6} {a.through:>6} {a.right:>6} {a.total:>6}"
                      f"  {lane_str:>14}  {a.saturation_flow():>8.0f}  {a.flow_ratio():>.4f}"
                      f"  {a.peds_conflicting:>5}")

        # Solve timing
        result = ix.solve_timing()

        if not result["feasible"]:
            print(f"\n  *** {result['message']}")
            continue

        C = result["cycle_length"]
        print(f"\n  Y_c = {result['Y_c']:.3f}  |  "
              f"Webster C_opt = {result['cycle_webster']:.0f}s  |  "
              f"Cycle = {C:.0f}s  |  "
              f"Lost = {result['total_lost']:.0f}s  |  "
              f"Phases = {len(result['phases'])}")

        for i, phase in enumerate(result["phases"]):
            dirs = " + ".join(phase["directions"])
            vc_str = ", ".join(f"{d}={vc:.2f}" for d, vc in phase["vc_ratios"].items())
            print(f"    Phase {i+1}: {dirs:<16}  "
                  f"green = {phase['green']:>5.1f}s  "
                  f"y_c = {phase['critical_y']:.4f}  "
                  f"v/c: {vc_str}")

    # ── Corridor summary ─────────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("CORRIDOR SUMMARY — GREEN WAVE FEASIBILITY")
    print(f"{'=' * 100}")
    print(f"\n  {'Intersection':<45} {'Lat':>10} {'Cycle':>6} {'NB green':>9}"
          f" {'NB v/c':>7} {'Spacing':>8}")

    prev_lat = None
    for ix in corridor:
        result = ix.solve_timing()
        if not result["feasible"]:
            c_str = "FAIL"
            nb_g = "-"
            nb_vc = "-"
        else:
            c_str = f"{result['cycle_length']:.0f}s"
            # Find NB green
            nb_g = "-"
            nb_vc = "-"
            for phase in result["phases"]:
                if "NB" in phase["directions"]:
                    nb_g = f"{phase['green']:.0f}s"
                    nb_vc = f"{phase['vc_ratios'].get('NB', 0):.2f}"

        # Distance to previous intersection
        if prev_lat is not None:
            dlat = prev_lat - ix.lat
            dist_m = dlat * 111_139  # approximate meters per degree lat
            dist_str = f"{dist_m:.0f}m"
        else:
            dist_str = "-"
        prev_lat = ix.lat

        name = ix.name[:44]
        print(f"  {name:<45} {ix.lat:>10.5f} {c_str:>6} {nb_g:>9} {nb_vc:>7} {dist_str:>8}")

    # ── Green wave analysis ──────────────────────────────────────────────
    print(f"\n{'=' * 100}")
    print("GREEN WAVE OFFSET ESTIMATION")
    print(f"{'=' * 100}")

    # For a green wave, consecutive intersections need coordinated offsets.
    # Offset = travel_time between intersections = distance / speed
    # A green wave works if all intersections share the same cycle length.
    ASSUMED_SPEED_MPH = 25  # typical DTLA arterial speed
    speed_mps = ASSUMED_SPEED_MPH * 0.44704

    print(f"\n  Assumed arterial speed: {ASSUMED_SPEED_MPH} mph ({speed_mps:.1f} m/s)")
    print(f"\n  {'From':<25} {'To':<25} {'Dist':>6} {'Travel':>7} {'Offset':>7}")

    prev = None
    for ix in corridor:
        if prev is not None:
            dlat = prev.lat - ix.lat
            dist_m = dlat * 111_139
            travel_s = dist_m / speed_mps
            # Offset as fraction of typical cycle
            result = ix.solve_timing()
            if result["feasible"]:
                offset_pct = (travel_s / result["cycle_length"]) * 100
            else:
                offset_pct = 0
            pn = prev.name.split(" at ")[0] if " at " in prev.name else prev.name[:24]
            cn = ix.name.split(" at ")[0] if " at " in ix.name else ix.name[:24]
            pn = pn.split(" AT ")[0] if " AT " in pn else pn
            cn = cn.split(" AT ")[0] if " AT " in cn else cn
            print(f"  {pn:<25} {cn:<25} {dist_m:>5.0f}m {travel_s:>6.1f}s {offset_pct:>6.1f}%")
        prev = ix

    print(f"\n  NOTE: A green wave requires all intersections to share the same cycle")
    print(f"  length. If cycles differ, the wave degrades over multiple cycles.")
    print(f"  Google Maps live data could validate these offsets by comparing")
    print(f"  congestion onset/clearing patterns across consecutive intersections.")


if __name__ == "__main__":
    main()
