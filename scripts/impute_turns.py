#!/usr/bin/env python3
"""Impute missing turn breakdowns in manual_counts.

Finds rows where total > 0 but left_turn = through = right_turn = 0,
then fills in turn splits using:
  1. Historical proportions from the same intersection/approach (different count date)
  2. Default proportions by direction if no historical data exists:
     - NB/SB: left=8%, through=85%, right=7%
     - EB/WB: left=15%, through=70%, right=15%
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "navigatela.db"

# Default proportions when no historical data exists
DEFAULTS = {
    "NB": (0.08, 0.85, 0.07),
    "SB": (0.08, 0.85, 0.07),
    "EB": (0.15, 0.70, 0.15),
    "WB": (0.15, 0.70, 0.15),
}


def split_turns(total, left_pct, through_pct, right_pct):
    """Split total into left/through/right, adjusting through for rounding."""
    left = round(total * left_pct)
    right = round(total * right_pct)
    through = total - left - right  # absorb rounding error
    return left, through, right


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(DB_PATH)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Find rows needing imputation
    missing = cur.execute("""
        SELECT id, traffic_id, approach, pdf_id, total
        FROM manual_counts
        WHERE total > 0
          AND left_turn = 0 AND "through" = 0 AND right_turn = 0
    """).fetchall()

    print(f"Rows needing imputation: {len(missing)}")

    from_historical = 0
    from_defaults = 0

    for row in missing:
        rid, traffic_id, approach, pdf_id, total = (
            row["id"], row["traffic_id"], row["approach"],
            row["pdf_id"], row["total"],
        )

        # Try historical proportions from same intersection + approach
        hist = cur.execute("""
            SELECT left_turn, "through", right_turn, total
            FROM manual_counts
            WHERE traffic_id = ? AND approach = ? AND pdf_id <> ?
              AND left_turn + "through" + right_turn > 0
              AND total > 50
            ORDER BY date DESC
            LIMIT 1
        """, (traffic_id, approach, pdf_id)).fetchone()

        if hist and hist["total"] > 0:
            hist_total = hist["total"]
            left_pct = hist["left_turn"] / hist_total
            right_pct = hist["right_turn"] / hist_total
            through_pct = hist["through"] / hist_total
            left, through, right = split_turns(total, left_pct, through_pct, right_pct)
            from_historical += 1
        else:
            if approach not in DEFAULTS:
                print(f"  WARNING: unknown approach '{approach}' for id={rid}, skipping")
                continue
            left_pct, through_pct, right_pct = DEFAULTS[approach]
            left, through, right = split_turns(total, left_pct, through_pct, right_pct)
            from_defaults += 1

        cur.execute("""
            UPDATE manual_counts
            SET left_turn = ?, "through" = ?, right_turn = ?
            WHERE id = ?
        """, (left, through, right, rid))

    conn.commit()
    conn.close()

    print(f"Imputed from historical proportions: {from_historical}")
    print(f"Imputed from default proportions:    {from_defaults}")
    print(f"Total imputed:                       {from_historical + from_defaults}")


if __name__ == "__main__":
    main()
