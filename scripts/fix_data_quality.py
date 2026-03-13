#!/usr/bin/env python3
"""Fix remaining data quality issues in navigatela.db for production readiness."""

import sqlite3
import re
from datetime import datetime, date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "navigatela.db"


def fix1_intra_pdf_duplicates(cur):
    """Remove duplicate rows within same PDF for same (hour_start, approach/crossing)."""
    print("\n=== Fix 1: Intra-PDF Duplicate Rows ===")

    # manual_counts: keep row with largest total per (pdf_id, hour_start, approach)
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT pdf_id, hour_start, approach FROM manual_counts
            GROUP BY pdf_id, hour_start, approach HAVING COUNT(*) > 1
        )
    """)
    dup_groups = cur.fetchone()[0]
    print(f"  manual_counts duplicate groups: {dup_groups}")

    cur.execute("""
        DELETE FROM manual_counts WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY pdf_id, hour_start, approach
                    ORDER BY total DESC, id
                ) as rn
                FROM manual_counts
            ) WHERE rn = 1
        )
    """)
    print(f"  manual_counts rows deleted: {cur.rowcount}")

    # manual_ped_counts: keep row with largest pedestrians per (pdf_id, hour_start, crossing)
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT pdf_id, hour_start, crossing FROM manual_ped_counts
            GROUP BY pdf_id, hour_start, crossing HAVING COUNT(*) > 1
        )
    """)
    ped_dup_groups = cur.fetchone()[0]
    print(f"  manual_ped_counts duplicate groups: {ped_dup_groups}")

    cur.execute("""
        DELETE FROM manual_ped_counts WHERE id NOT IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY pdf_id, hour_start, crossing
                    ORDER BY pedestrians DESC, id
                ) as rn
                FROM manual_ped_counts
            ) WHERE rn = 1
        )
    """)
    print(f"  manual_ped_counts rows deleted: {cur.rowcount}")


def fix2_auto_total_mismatch(cur):
    """Fix automatic_counts where total doesn't match nw_total + se_total."""
    print("\n=== Fix 2: Auto Direction Total Mismatch ===")

    cur.execute("""
        SELECT COUNT(*) FROM automatic_counts
        WHERE total != nw_total + se_total
        AND nw_total IS NOT NULL AND se_total IS NOT NULL
    """)
    mismatch = cur.fetchone()[0]
    print(f"  Rows with total != nw_total + se_total: {mismatch}")

    cur.execute("""
        UPDATE automatic_counts
        SET total = nw_total + se_total
        WHERE total != nw_total + se_total
        AND nw_total IS NOT NULL AND se_total IS NOT NULL
    """)
    print(f"  Rows updated: {cur.rowcount}")


def extract_date_from_filename(filename):
    """Extract a date from a PDF filename.

    Patterns:
    - 6-digit YYMMDD embedded in the name: BEVVER110407.pdf -> 2011-04-07
    - Dotted format: AVENUE51.FIGUEROA.141118-MIO.pdf -> 2014-11-18
    - Dotted format: DESOTO.DEVONSHIRE.190529.MAN.pdf -> 2019-05-29
    """
    stem = Path(filename).stem

    # Find all 6-digit sequences that could be YYMMDD
    # Look for them in order of specificity
    candidates = []

    # Pattern: 6 digits preceded by non-digit (or start) and followed by non-digit/end/underscore/dash
    for m in re.finditer(r'(?<!\d)(\d{6})(?!\d)', stem):
        digits = m.group(1)
        yy = int(digits[0:2])
        mm = int(digits[2:4])
        dd = int(digits[4:6])

        # Validate month and day
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            continue

        # Convert 2-digit year: 90-99 -> 1990s, 00-30 -> 2000s
        year = 1900 + yy if yy >= 90 else 2000 + yy

        try:
            d = date(year, mm, dd)
            # Sanity check: not in the future and not before 1990
            if date(1990, 1, 1) <= d <= date(2026, 12, 31):
                candidates.append(d)
        except ValueError:
            continue

    # Also check for 8-digit YYYYMMDD patterns
    for m in re.finditer(r'(?<!\d)(\d{8})(?!\d)', stem):
        digits = m.group(1)
        year = int(digits[0:4])
        mm = int(digits[4:6])
        dd = int(digits[6:8])
        if not (1990 <= year <= 2026 and 1 <= mm <= 12 and 1 <= dd <= 31):
            continue
        try:
            d = date(year, mm, dd)
            candidates.append(d)
        except ValueError:
            continue

    if candidates:
        # Prefer the last match (usually the most specific/date-like part)
        return candidates[-1]
    return None


DOW_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday"
]


def fix3_null_dates(cur):
    """Recover dates from PDF filenames where date is NULL."""
    print("\n=== Fix 3: NULL Dates ===")

    # Get all PDFs that have NULL-date rows
    cur.execute("""
        SELECT DISTINCT p.id, p.filename FROM pdfs p
        WHERE p.id IN (
            SELECT pdf_id FROM manual_counts WHERE date IS NULL
            UNION
            SELECT pdf_id FROM automatic_counts WHERE date IS NULL
        )
    """)
    pdfs = cur.fetchall()
    print(f"  PDFs with NULL-date rows: {len(pdfs)}")

    recovered = 0
    failed = 0
    for pdf_id, filename in pdfs:
        d = extract_date_from_filename(filename)
        if d is None:
            failed += 1
            continue

        date_str = d.isoformat()
        dow = DOW_NAMES[d.weekday()]

        cur.execute("""
            UPDATE manual_counts SET date = ?, day_of_week = ?
            WHERE pdf_id = ? AND date IS NULL
        """, (date_str, dow, pdf_id))
        manual_updated = cur.rowcount

        cur.execute("""
            UPDATE automatic_counts SET date = ?, day_of_week = ?
            WHERE pdf_id = ? AND date IS NULL
        """, (date_str, dow, pdf_id))
        auto_updated = cur.rowcount

        if manual_updated + auto_updated > 0:
            recovered += 1

    print(f"  PDFs with date recovered: {recovered}")
    print(f"  PDFs where filename had no extractable date: {failed}")

    # Report remaining NULLs
    cur.execute("SELECT COUNT(*) FROM manual_counts WHERE date IS NULL")
    print(f"  Remaining manual NULL dates: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM automatic_counts WHERE date IS NULL")
    print(f"  Remaining auto NULL dates: {cur.fetchone()[0]}")


def fix4_garbled_street_names(cur):
    """Fix garbled street names using intersections table."""
    print("\n=== Fix 4: Garbled Street Names ===")

    # Get PDFs with garbled names and their intersection data
    cur.execute("""
        SELECT DISTINCT mc.pdf_id, mc.traffic_id, i.intersection
        FROM manual_counts mc
        JOIN intersections i ON mc.traffic_id = i.traffic_id
        WHERE LENGTH(mc.ns_street) > 50 OR LENGTH(mc.ew_street) > 50
    """)
    garbled = cur.fetchall()
    print(f"  PDFs with garbled names: {len(garbled)}")

    fixed = 0
    for pdf_id, traffic_id, intersection in garbled:
        # Split intersection on " at " or " AT "
        parts = re.split(r'\s+[aA][tT]\s+', intersection, maxsplit=1)
        if len(parts) != 2:
            print(f"    WARNING: Can't split intersection '{intersection}' for pdf_id={pdf_id}")
            continue

        # In LADOT convention, the first street in "X at Y" is typically the
        # NS street and the second is EW, but the original data may have been
        # in either order. Use the intersection as canonical.
        street1, street2 = parts[0].strip(), parts[1].strip()

        cur.execute("""
            UPDATE manual_counts SET ns_street = ?, ew_street = ?
            WHERE pdf_id = ? AND (LENGTH(ns_street) > 50 OR LENGTH(ew_street) > 50)
        """, (street1, street2, pdf_id))
        if cur.rowcount > 0:
            fixed += 1

    print(f"  PDFs fixed: {fixed}")

    # Verify
    cur.execute("""
        SELECT COUNT(DISTINCT pdf_id) FROM manual_counts
        WHERE LENGTH(ns_street) > 50 OR LENGTH(ew_street) > 50
    """)
    remaining = cur.fetchone()[0]
    print(f"  Remaining garbled (no intersection match): {remaining}")


def fix5_zero_total_ped_only(cur):
    """Delete manual_counts rows where entire PDF has SUM(total) = 0."""
    print("\n=== Fix 5: Zero-Total Ped-Only Rows ===")

    cur.execute("""
        SELECT COUNT(*) FROM manual_counts
        WHERE pdf_id IN (
            SELECT pdf_id FROM manual_counts GROUP BY pdf_id HAVING SUM(total) = 0
        )
    """)
    rows = cur.fetchone()[0]
    print(f"  Rows in all-zero PDFs: {rows}")

    cur.execute("""
        DELETE FROM manual_counts
        WHERE pdf_id IN (
            SELECT pdf_id FROM manual_counts GROUP BY pdf_id HAVING SUM(total) = 0
        )
    """)
    print(f"  Rows deleted: {cur.rowcount}")


def fix6_null_day_of_week(cur):
    """Recompute day_of_week from date where DOW is NULL or garbled."""
    print("\n=== Fix 6: NULL/Garbled day_of_week ===")

    # Fix garbled DOW values first
    cur.execute("""
        SELECT DISTINCT day_of_week FROM manual_counts
        WHERE day_of_week IS NOT NULL
        AND day_of_week NOT IN ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
    """)
    garbled_dows = [r[0] for r in cur.fetchall()]
    if garbled_dows:
        print(f"  Garbled DOW values found: {garbled_dows}")
        for gdow in garbled_dows:
            # Try to match to a valid DOW
            clean = gdow.strip("'\"").strip()
            matched = None
            for dow in DOW_NAMES:
                if clean.lower() in dow.lower() or dow.lower().startswith(clean.lower()):
                    matched = dow
                    break
            if matched:
                cur.execute(
                    "UPDATE manual_counts SET day_of_week = ? WHERE day_of_week = ?",
                    (matched, gdow)
                )
                print(f"    '{gdow}' -> '{matched}': {cur.rowcount} rows")

    # Now compute DOW from date for remaining NULLs
    cur.execute("""
        SELECT DISTINCT date FROM manual_counts
        WHERE date IS NOT NULL AND day_of_week IS NULL
    """)
    dates = cur.fetchall()
    print(f"  Distinct dates needing DOW computation: {len(dates)}")

    updated = 0
    for (date_str,) in dates:
        try:
            d = date.fromisoformat(date_str)
            dow = DOW_NAMES[d.weekday()]
            cur.execute(
                "UPDATE manual_counts SET day_of_week = ? WHERE date = ? AND day_of_week IS NULL",
                (dow, date_str)
            )
            updated += cur.rowcount
        except (ValueError, TypeError):
            pass

    print(f"  Rows updated with computed DOW: {updated}")

    # Also fix the bad date 0202-06-16
    cur.execute("SELECT COUNT(*) FROM manual_counts WHERE date LIKE '02%' AND date < '1990-01-01'")
    bad_dates = cur.fetchone()[0]
    if bad_dates > 0:
        print(f"  Rows with implausible date (< 1990): {bad_dates}")
        cur.execute("UPDATE manual_counts SET date = NULL WHERE date < '1990-01-01'")
        print(f"    Set to NULL: {cur.rowcount}")

    # Final NULL counts
    cur.execute("SELECT COUNT(*) FROM manual_counts WHERE day_of_week IS NULL")
    print(f"  Remaining manual NULL DOW: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM automatic_counts WHERE day_of_week IS NULL")
    print(f"  Remaining auto NULL DOW: {cur.fetchone()[0]}")


def verify(cur):
    """Run verification queries."""
    print("\n=== Verification ===")

    # Row counts
    cur.execute("SELECT COUNT(*) FROM manual_counts")
    print(f"  manual_counts total rows: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM automatic_counts")
    print(f"  automatic_counts total rows: {cur.fetchone()[0]}")
    cur.execute("SELECT COUNT(*) FROM manual_ped_counts")
    print(f"  manual_ped_counts total rows: {cur.fetchone()[0]}")

    # No remaining duplicates
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT pdf_id, hour_start, approach FROM manual_counts
            GROUP BY pdf_id, hour_start, approach HAVING COUNT(*) > 1
        )
    """)
    val = cur.fetchone()[0]
    print(f"  manual_counts remaining duplicate groups: {val} {'OK' if val == 0 else 'FAIL'}")

    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT pdf_id, hour_start, crossing FROM manual_ped_counts
            GROUP BY pdf_id, hour_start, crossing HAVING COUNT(*) > 1
        )
    """)
    val = cur.fetchone()[0]
    print(f"  manual_ped_counts remaining duplicate groups: {val} {'OK' if val == 0 else 'FAIL'}")

    # No auto total mismatches
    cur.execute("""
        SELECT COUNT(*) FROM automatic_counts
        WHERE total != nw_total + se_total
        AND nw_total IS NOT NULL AND se_total IS NOT NULL
    """)
    val = cur.fetchone()[0]
    print(f"  auto total mismatches: {val} {'OK' if val == 0 else 'FAIL'}")

    # NULL dates
    cur.execute("SELECT COUNT(*) FROM manual_counts WHERE date IS NULL")
    manual_null = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM manual_counts")
    manual_total = cur.fetchone()[0]
    pct = (1 - manual_null / manual_total) * 100 if manual_total > 0 else 0
    print(f"  manual NULL dates: {manual_null} ({pct:.1f}% have dates)")

    cur.execute("SELECT COUNT(*) FROM automatic_counts WHERE date IS NULL")
    auto_null = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM automatic_counts")
    auto_total = cur.fetchone()[0]
    pct = (1 - auto_null / auto_total) * 100 if auto_total > 0 else 0
    print(f"  auto NULL dates: {auto_null} ({pct:.1f}% have dates)")

    # Garbled street names
    cur.execute("""
        SELECT COUNT(DISTINCT pdf_id) FROM manual_counts
        WHERE LENGTH(ns_street) > 50 OR LENGTH(ew_street) > 50
    """)
    val = cur.fetchone()[0]
    print(f"  PDFs with garbled street names (>50 chars): {val} {'OK' if val == 0 else 'FAIL'}")

    # Zero-total PDFs
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT pdf_id FROM manual_counts GROUP BY pdf_id HAVING SUM(total) = 0
        )
    """)
    val = cur.fetchone()[0]
    print(f"  PDFs with all-zero totals: {val} {'OK' if val == 0 else 'FAIL'}")

    # NULL day_of_week
    cur.execute("SELECT COUNT(*) FROM manual_counts WHERE day_of_week IS NULL")
    print(f"  manual NULL day_of_week: {cur.fetchone()[0]}")

    # Garbled DOW
    cur.execute("""
        SELECT COUNT(*) FROM manual_counts
        WHERE day_of_week IS NOT NULL
        AND day_of_week NOT IN ('Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday')
    """)
    val = cur.fetchone()[0]
    print(f"  Garbled day_of_week values: {val} {'OK' if val == 0 else 'FAIL'}")

    # Hour distribution
    print("\n  Hour distribution (manual_counts):")
    cur.execute("""
        SELECT hour_start, COUNT(*) as cnt FROM manual_counts
        GROUP BY hour_start ORDER BY hour_start
    """)
    for hour, cnt in cur.fetchall():
        print(f"    hour {hour:2d}: {cnt:,}")


def main():
    print(f"Database: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # Enable WAL for safety
    cur.execute("PRAGMA journal_mode=WAL")

    fix1_intra_pdf_duplicates(cur)
    fix2_auto_total_mismatch(cur)
    fix3_null_dates(cur)
    fix4_garbled_street_names(cur)
    fix5_zero_total_ped_only(cur)
    fix6_null_day_of_week(cur)

    verify(cur)

    conn.commit()
    print("\nAll fixes committed successfully.")
    conn.close()


if __name__ == "__main__":
    main()
