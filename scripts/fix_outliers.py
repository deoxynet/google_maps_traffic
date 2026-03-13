#!/usr/bin/env python3
"""Fix remaining outlier/consistency issues in navigatela.db."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "navigatela.db"


def fix_manual_total_consistency(cur):
    """Fix manual_counts where total != left+through+right."""
    print("=== Fix: Manual count total consistency ===")

    # Pattern 1: total < components (total is wrong) — 857 rows
    cur.execute("""
        UPDATE manual_counts
        SET total = left_turn + through + right_turn
        WHERE total < left_turn + through + right_turn
        AND left_turn IS NOT NULL AND through IS NOT NULL AND right_turn IS NOT NULL
        AND left_turn + through + right_turn > 0
    """)
    print(f"  total < components (set total = sum): {cur.rowcount} rows")

    # Pattern 2: components > 0 but total = 0 — 11 rows
    cur.execute("""
        UPDATE manual_counts
        SET total = left_turn + through + right_turn
        WHERE total = 0
        AND left_turn IS NOT NULL AND through IS NOT NULL AND right_turn IS NOT NULL
        AND left_turn + through + right_turn > 0
    """)
    print(f"  components>0, total=0 (set total = sum): {cur.rowcount} rows")

    # Pattern 3: total >> components by >100 (663 rows) — total came from wrong line
    # Trust the detailed component breakdown over a garbled total
    cur.execute("""
        UPDATE manual_counts
        SET total = left_turn + through + right_turn
        WHERE total > left_turn + through + right_turn
        AND left_turn + through + right_turn > 0
        AND total - (left_turn + through + right_turn) > 100
        AND left_turn IS NOT NULL AND through IS NOT NULL AND right_turn IS NOT NULL
    """)
    print(f"  total >> components by >100 (set total = sum): {cur.rowcount} rows")

    # Verify
    cur.execute("""
        SELECT COUNT(*) FROM manual_counts
        WHERE total != left_turn + through + right_turn
        AND left_turn IS NOT NULL AND through IS NOT NULL AND right_turn IS NOT NULL
        AND left_turn + through + right_turn > 0
    """)
    remaining = cur.fetchone()[0]
    print(f"  Remaining mismatches (excess ≤100, likely U-turns): {remaining}")


def fix_auto_quarter_column_shift(cur):
    """Fix auto counts where parser captured subtotal as q4 and real q4 as dir_total."""
    print("\n=== Fix: Auto quarter-hour column shift ===")

    # NW direction: where q4 == q1+q2+q3 (confirms column shift)
    cur.execute("""
        SELECT COUNT(*) FROM automatic_counts
        WHERE nw_q4 = nw_q1 + nw_q2 + nw_q3
        AND nw_q1 IS NOT NULL AND nw_q2 IS NOT NULL AND nw_q3 IS NOT NULL AND nw_q4 IS NOT NULL
        AND nw_total != nw_q1 + nw_q2 + nw_q3 + nw_q4
    """)
    nw_shifted = cur.fetchone()[0]
    print(f"  NW column-shifted rows detected: {nw_shifted}")

    # Fix: real_q4 = nw_total, real_nw_total = q1+q2+q3+real_q4
    cur.execute("""
        UPDATE automatic_counts
        SET nw_q4 = nw_total,
            nw_total = nw_q1 + nw_q2 + nw_q3 + nw_total
        WHERE nw_q4 = nw_q1 + nw_q2 + nw_q3
        AND nw_q1 IS NOT NULL AND nw_q2 IS NOT NULL AND nw_q3 IS NOT NULL AND nw_q4 IS NOT NULL
        AND nw_total != nw_q1 + nw_q2 + nw_q3 + nw_q4
    """)
    print(f"  NW fixed: {cur.rowcount} rows")

    # SE direction: same pattern
    cur.execute("""
        SELECT COUNT(*) FROM automatic_counts
        WHERE se_q4 = se_q1 + se_q2 + se_q3
        AND se_q1 IS NOT NULL AND se_q2 IS NOT NULL AND se_q3 IS NOT NULL AND se_q4 IS NOT NULL
        AND se_total != se_q1 + se_q2 + se_q3 + se_q4
    """)
    se_shifted = cur.fetchone()[0]
    print(f"  SE column-shifted rows detected: {se_shifted}")

    cur.execute("""
        UPDATE automatic_counts
        SET se_q4 = se_total,
            se_total = se_q1 + se_q2 + se_q3 + se_total
        WHERE se_q4 = se_q1 + se_q2 + se_q3
        AND se_q1 IS NOT NULL AND se_q2 IS NOT NULL AND se_q3 IS NOT NULL AND se_q4 IS NOT NULL
        AND se_total != se_q1 + se_q2 + se_q3 + se_q4
    """)
    print(f"  SE fixed: {cur.rowcount} rows")

    # Now fix remaining quarter mismatches by trusting quarters over totals
    cur.execute("""
        UPDATE automatic_counts
        SET nw_total = nw_q1 + nw_q2 + nw_q3 + nw_q4
        WHERE nw_q1 IS NOT NULL AND nw_q2 IS NOT NULL AND nw_q3 IS NOT NULL AND nw_q4 IS NOT NULL
        AND nw_total != nw_q1 + nw_q2 + nw_q3 + nw_q4
    """)
    print(f"  NW remaining quarter mismatches fixed: {cur.rowcount} rows")

    cur.execute("""
        UPDATE automatic_counts
        SET se_total = se_q1 + se_q2 + se_q3 + se_q4
        WHERE se_q1 IS NOT NULL AND se_q2 IS NOT NULL AND se_q3 IS NOT NULL AND se_q4 IS NOT NULL
        AND se_total != se_q1 + se_q2 + se_q3 + se_q4
    """)
    print(f"  SE remaining quarter mismatches fixed: {cur.rowcount} rows")

    # Recompute total = nw_total + se_total for all affected
    cur.execute("""
        UPDATE automatic_counts
        SET total = nw_total + se_total
        WHERE total != nw_total + se_total
        AND nw_total IS NOT NULL AND se_total IS NOT NULL
    """)
    print(f"  Recomputed total = nw+se: {cur.rowcount} rows")


def fix_extreme_manual_outliers(cur):
    """Identify and fix clearly garbled extreme values in manual counts."""
    print("\n=== Fix: Extreme manual count outliers ===")

    # Pattern: all volume in left_turn, through=0, right=0, and left_turn > 4000
    # These are single-movement approaches where parser put total in left_turn
    # The total is correct but the turn breakdown is wrong
    # For simulation: total is what matters, zero out the fake turn breakdown
    cur.execute("""
        SELECT COUNT(*) FROM manual_counts
        WHERE left_turn > 4000 AND through <= 1 AND right_turn <= 1
    """)
    fake_left = cur.fetchone()[0]
    print(f"  Rows with all volume in left_turn (>4000, T≈0, R≈0): {fake_left}")

    # Set these to: through=total, left=0, right=0 (treat as through movement)
    # since a 100% left-turn approach at >4000 veh/hr is not physically possible
    cur.execute("""
        UPDATE manual_counts
        SET through = total, left_turn = 0, right_turn = 0
        WHERE left_turn > 4000 AND through <= 1 AND right_turn <= 1
    """)
    print(f"  Reclassified as through movement: {cur.rowcount} rows")

    # Total-only rows with extreme values (>p99.5 ≈ 2800) that have
    # L+T+R=0 — these are likely page totals captured as data
    # Check if other approaches exist for same PDF/hour
    cur.execute("""
        SELECT mc.id, mc.pdf_id, mc.hour_start, mc.approach, mc.total,
          (SELECT COUNT(*) FROM manual_counts m2
           WHERE m2.pdf_id = mc.pdf_id AND m2.hour_start = mc.hour_start
           AND m2.id != mc.id) as sibling_count
        FROM manual_counts mc
        WHERE mc.left_turn = 0 AND mc.through = 0 AND mc.right_turn = 0
        AND mc.total > 2800
    """)
    extreme_total_only = cur.fetchall()

    # If there are sibling rows for the same hour, this is likely a
    # summary/total row that duplicates data — delete it
    ids_to_delete = [r[0] for r in extreme_total_only if r[5] > 0]
    print(f"  Extreme total-only rows (>2800) with siblings (page totals): {len(ids_to_delete)}")
    if ids_to_delete:
        cur.execute(
            f"DELETE FROM manual_counts WHERE id IN ({','.join(str(i) for i in ids_to_delete)})"
        )
        print(f"  Deleted: {cur.rowcount} rows")

    # Rows where total-only AND no siblings — these are legitimate single-approach counts
    legit = [r for r in extreme_total_only if r[5] == 0]
    print(f"  Extreme total-only with no siblings (kept): {len(legit)}")


def verify(cur):
    """Run final verification."""
    print("\n=== Final Verification ===")

    cur.execute("SELECT COUNT(*) FROM manual_counts")
    print(f"  manual_counts rows: {cur.fetchone()[0]:,}")
    cur.execute("SELECT COUNT(*) FROM automatic_counts")
    print(f"  automatic_counts rows: {cur.fetchone()[0]:,}")

    # Manual sum consistency
    cur.execute("""
        SELECT COUNT(*) FROM manual_counts
        WHERE total != left_turn + through + right_turn
        AND left_turn IS NOT NULL AND through IS NOT NULL AND right_turn IS NOT NULL
        AND left_turn + through + right_turn > 0
        AND total - (left_turn + through + right_turn) > 100
    """)
    print(f"  Manual total mismatch (>100): {cur.fetchone()[0]}")

    # Auto quarter consistency
    cur.execute("""
        SELECT COUNT(*) FROM automatic_counts
        WHERE nw_q1 IS NOT NULL AND nw_q2 IS NOT NULL AND nw_q3 IS NOT NULL AND nw_q4 IS NOT NULL
        AND nw_total != nw_q1+nw_q2+nw_q3+nw_q4
    """)
    nw = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM automatic_counts
        WHERE se_q1 IS NOT NULL AND se_q2 IS NOT NULL AND se_q3 IS NOT NULL AND se_q4 IS NOT NULL
        AND se_total != se_q1+se_q2+se_q3+se_q4
    """)
    se = cur.fetchone()[0]
    print(f"  Auto quarter mismatches (nw/se): {nw}/{se}")

    # Auto total consistency
    cur.execute("""
        SELECT COUNT(*) FROM automatic_counts
        WHERE total != nw_total + se_total AND nw_total IS NOT NULL AND se_total IS NOT NULL
    """)
    print(f"  Auto total != nw+se: {cur.fetchone()[0]}")

    # Distribution stats
    print("\n  --- Manual total percentiles ---")
    cur.execute("""
        SELECT percentile, MAX(total) as val FROM (
            SELECT total, NTILE(100) OVER (ORDER BY total) as percentile
            FROM manual_counts
        ) GROUP BY percentile
        HAVING percentile IN (50, 75, 90, 95, 99, 100)
    """)
    for pct, val in cur.fetchall():
        print(f"    p{pct}: {val:,}")

    print("\n  --- Auto total percentiles ---")
    cur.execute("""
        SELECT percentile, MAX(total) as val FROM (
            SELECT total, NTILE(100) OVER (ORDER BY total) as percentile
            FROM automatic_counts
        ) GROUP BY percentile
        HAVING percentile IN (50, 75, 90, 95, 99, 100)
    """)
    for pct, val in cur.fetchall():
        print(f"    p{pct}: {val:,}")

    # Extreme values remaining
    print("\n  --- Remaining extremes ---")
    cur.execute("SELECT COUNT(*) FROM manual_counts WHERE total > 5000")
    print(f"  Manual approach total > 5000: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM manual_counts WHERE total > 3000")
    print(f"  Manual approach total > 3000: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM automatic_counts WHERE total > 10000")
    print(f"  Auto hourly total > 10000: {cur.fetchone()[0]}")


def main():
    print(f"Database: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")

    fix_manual_total_consistency(cur)
    fix_auto_quarter_column_shift(cur)
    fix_extreme_manual_outliers(cur)
    verify(cur)

    conn.commit()
    print("\nAll outlier fixes committed.")
    conn.close()


if __name__ == "__main__":
    main()
