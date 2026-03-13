#!/usr/bin/env python3
"""Deduplicate intersections within ~30m of each other in navigatela.db.

Finds clusters of intersections within 0.0003 degrees (~30m), picks a winner
for each cluster based on data recency and approach coverage, then merges
all count/timing data to the winner and removes the losers.
"""

import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "navigatela.db"
THRESHOLD = 0.0003  # ~30 meters in degrees


def find_pairs(cur):
    """Find all pairs of intersections within the distance threshold."""
    cur.execute("""
        SELECT a.traffic_id, b.traffic_id
        FROM intersections a
        JOIN intersections b
          ON a.traffic_id < b.traffic_id
         AND abs(a.lat - b.lat) < ?
         AND abs(a.lon - b.lon) < ?
    """, (THRESHOLD, THRESHOLD))
    return cur.fetchall()


def union_find_clusters(pairs):
    """Group pairs into clusters using union-find."""
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b in pairs:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        union(a, b)

    clusters = defaultdict(set)
    for node in parent:
        clusters[find(node)].add(node)
    return list(clusters.values())


def pick_winner(cur, cluster):
    """Pick the best traffic_id to keep from a cluster.

    Prefers: most recent count data, then most approach coverage, then lowest id.
    """
    scores = []
    for tid in cluster:
        cur.execute("""
            SELECT MAX(date), COUNT(DISTINCT approach)
            FROM manual_counts WHERE traffic_id = ?
        """, (tid,))
        max_date, n_approaches = cur.fetchone()
        max_date = max_date or ""
        n_approaches = n_approaches or 0
        scores.append((max_date, n_approaches, -tid, tid))

    scores.sort(reverse=True)
    winner = scores[0][3]
    losers = [s[3] for s in scores[1:]]
    return winner, losers


def merge_cluster(cur, winner, losers):
    """Reassign all data from losers to winner, then delete losers."""
    merged_counts = {"manual_counts": 0, "automatic_counts": 0, "manual_ped_counts": 0}

    for loser in losers:
        for table in ("manual_counts", "automatic_counts", "manual_ped_counts"):
            cur.execute(f"UPDATE {table} SET traffic_id = ? WHERE traffic_id = ?",
                        (winner, loser))
            merged_counts[table] += cur.rowcount

        # Delete signal_timing for the loser (don't merge -- timing is intersection-specific)
        cur.execute("DELETE FROM signal_timing WHERE traffic_id = ?", (loser,))

        # Delete the loser intersection
        cur.execute("DELETE FROM intersections WHERE traffic_id = ?", (loser,))

    return merged_counts


def dedup_manual_counts(cur):
    """Remove duplicate manual_counts rows created by merging.

    When two intersections are merged, they may have had overlapping
    pdf_id + hour_start + approach combos. Keep the row with the higher total.
    """
    cur.execute("""
        SELECT traffic_id, pdf_id, hour_start, approach, COUNT(*) as c
        FROM manual_counts
        GROUP BY traffic_id, pdf_id, hour_start, approach
        HAVING c > 1
    """)
    dup_groups = cur.fetchall()
    removed = 0

    for tid, pdf_id, hour_start, approach, count in dup_groups:
        # Keep the row with the highest total; if tied, keep the lowest id
        cur.execute("""
            SELECT id FROM manual_counts
            WHERE traffic_id = ? AND pdf_id = ? AND hour_start = ? AND approach = ?
            ORDER BY total DESC, id ASC
        """, (tid, pdf_id, hour_start, approach))
        rows = cur.fetchall()
        ids_to_delete = [r[0] for r in rows[1:]]
        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            cur.execute(f"DELETE FROM manual_counts WHERE id IN ({placeholders})",
                        ids_to_delete)
            removed += len(ids_to_delete)

    return removed


def dedup_automatic_counts(cur):
    """Remove duplicate automatic_counts rows created by merging."""
    cur.execute("""
        SELECT traffic_id, pdf_id, hour, COUNT(*) as c
        FROM automatic_counts
        GROUP BY traffic_id, pdf_id, hour
        HAVING c > 1
    """)
    dup_groups = cur.fetchall()
    removed = 0

    for tid, pdf_id, hour, count in dup_groups:
        cur.execute("""
            SELECT id FROM automatic_counts
            WHERE traffic_id = ? AND pdf_id = ? AND hour = ?
            ORDER BY total DESC, id ASC
        """, (tid, pdf_id, hour))
        rows = cur.fetchall()
        ids_to_delete = [r[0] for r in rows[1:]]
        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            cur.execute(f"DELETE FROM automatic_counts WHERE id IN ({placeholders})",
                        ids_to_delete)
            removed += len(ids_to_delete)

    return removed


def dedup_manual_ped_counts(cur):
    """Remove duplicate manual_ped_counts rows created by merging."""
    cur.execute("""
        SELECT traffic_id, pdf_id, hour_start, crossing, COUNT(*) as c
        FROM manual_ped_counts
        GROUP BY traffic_id, pdf_id, hour_start, crossing
        HAVING c > 1
    """)
    dup_groups = cur.fetchall()
    removed = 0

    for tid, pdf_id, hour_start, crossing, count in dup_groups:
        cur.execute("""
            SELECT id FROM manual_ped_counts
            WHERE traffic_id = ? AND pdf_id = ? AND hour_start = ? AND crossing = ?
            ORDER BY pedestrians DESC, id ASC
        """, (tid, pdf_id, hour_start, crossing))
        rows = cur.fetchall()
        ids_to_delete = [r[0] for r in rows[1:]]
        if ids_to_delete:
            placeholders = ",".join("?" * len(ids_to_delete))
            cur.execute(f"DELETE FROM manual_ped_counts WHERE id IN ({placeholders})",
                        ids_to_delete)
            removed += len(ids_to_delete)

    return removed


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(DB_PATH)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Count before
    cur.execute("SELECT COUNT(*) FROM intersections")
    before_intersections = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM manual_counts")
    before_mc = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM automatic_counts")
    before_ac = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM manual_ped_counts")
    before_mpc = cur.fetchone()[0]

    # Step 1: Find duplicate pairs
    pairs = find_pairs(cur)
    print(f"Found {len(pairs)} duplicate pairs")

    # Step 2: Cluster them using union-find
    clusters = union_find_clusters(pairs)
    multi = [c for c in clusters if len(c) > 1]
    print(f"Formed {len(multi)} clusters to merge "
          f"(sizes: {sorted([len(c) for c in multi], reverse=True)[:10]}...)")

    # Step 3: Merge each cluster
    total_merged = {"manual_counts": 0, "automatic_counts": 0, "manual_ped_counts": 0}
    losers_removed = 0
    for cluster in multi:
        winner, losers = pick_winner(cur, cluster)
        counts = merge_cluster(cur, winner, losers)
        for k in total_merged:
            total_merged[k] += counts[k]
        losers_removed += len(losers)

    print(f"\nMerged {losers_removed} loser intersections into winners")
    print(f"  Reassigned rows: manual_counts={total_merged['manual_counts']}, "
          f"automatic_counts={total_merged['automatic_counts']}, "
          f"manual_ped_counts={total_merged['manual_ped_counts']}")

    # Step 4: Dedup count rows created by merge
    mc_deduped = dedup_manual_counts(cur)
    ac_deduped = dedup_automatic_counts(cur)
    mpc_deduped = dedup_manual_ped_counts(cur)
    print(f"\nPost-merge dedup removed: "
          f"manual_counts={mc_deduped}, automatic_counts={ac_deduped}, "
          f"manual_ped_counts={mpc_deduped}")

    # Count after
    cur.execute("SELECT COUNT(*) FROM intersections")
    after_intersections = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM manual_counts")
    after_mc = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM automatic_counts")
    after_ac = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM manual_ped_counts")
    after_mpc = cur.fetchone()[0]

    print(f"\n--- Summary ---")
    print(f"Intersections: {before_intersections} -> {after_intersections} "
          f"(removed {before_intersections - after_intersections})")
    print(f"Manual counts: {before_mc} -> {after_mc} "
          f"(net change {after_mc - before_mc})")
    print(f"Automatic counts: {before_ac} -> {after_ac} "
          f"(net change {after_ac - before_ac})")
    print(f"Manual ped counts: {before_mpc} -> {after_mpc} "
          f"(net change {after_mpc - before_mpc})")

    # Verify no remaining nearby duplicates
    remaining = find_pairs(cur)
    print(f"\nRemaining nearby pairs after merge: {len(remaining)}")

    conn.commit()
    conn.close()
    print("Done. Changes committed to database.")


if __name__ == "__main__":
    main()
