#!/usr/bin/env python3
"""
NavigateLA Traffic Data -> SQLite Pipeline

Scrapes ~11,894 LADOT intersection traffic count records from NavigateLA's
ArcGIS MapServer, downloads and parses ~34,500 PDFs (automatic + manual counts),
and stores structured hourly directional traffic volumes in SQLite.

Usage:
    python scripts/scrape_navigatela.py --step index
    python scripts/scrape_navigatela.py --step scrape [--limit 100]
    python scripts/scrape_navigatela.py --step download [--limit 100]
    python scripts/scrape_navigatela.py --step parse-auto
    python scripts/scrape_navigatela.py --step parse-manual
    python scripts/scrape_navigatela.py --step all
"""

import argparse
import asyncio
import json
import logging
import re
import sqlite3
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp
import pdfplumber
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "navigatela.db"
PDF_AUTO_DIR = DATA_DIR / "pdfs" / "automatic"
PDF_MANUAL_DIR = DATA_DIR / "pdfs" / "manual"

LAYER176_PATH = Path("/tmp/navigatela_layer176.json")
LAYER177_PATH = Path("/tmp/navigatela_layer177.json")

REPORT_URL = "https://navigatela.lacity.org/reports/dot_traffic_data_report.cfm?trafficid={}"
SCRAPE_CONCURRENCY = 10
DOWNLOAD_CONCURRENCY = 20
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_DELAY = 2  # seconds between retries

log = logging.getLogger("navigatela")

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS intersections (
    traffic_id INTEGER PRIMARY KEY,
    intersection_id INTEGER,
    lat REAL,
    lon REAL,
    intersection TEXT
);

CREATE TABLE IF NOT EXISTS segments (
    object_id INTEGER PRIMARY KEY,
    asset_id INTEGER,
    segment_id INTEGER,
    location TEXT,
    geom_json TEXT
);

CREATE TABLE IF NOT EXISTS pdfs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    traffic_id INTEGER REFERENCES intersections,
    url TEXT UNIQUE,
    filename TEXT,
    type TEXT,
    downloaded INTEGER DEFAULT 0,
    parsed INTEGER DEFAULT 0,
    parse_error TEXT
);

CREATE TABLE IF NOT EXISTS automatic_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pdf_id INTEGER REFERENCES pdfs,
    traffic_id INTEGER REFERENCES intersections,
    location TEXT,
    date TEXT,
    day_of_week TEXT,
    weather TEXT,
    counter TEXT,
    sensor_layout TEXT,
    dot_district TEXT,
    hour INTEGER,
    nw_q1 INTEGER, nw_q2 INTEGER, nw_q3 INTEGER, nw_q4 INTEGER, nw_total INTEGER,
    se_q1 INTEGER, se_q2 INTEGER, se_q3 INTEGER, se_q4 INTEGER, se_total INTEGER,
    total INTEGER
);

CREATE TABLE IF NOT EXISTS manual_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pdf_id INTEGER REFERENCES pdfs,
    traffic_id INTEGER REFERENCES intersections,
    ns_street TEXT,
    ew_street TEXT,
    date TEXT,
    day_of_week TEXT,
    weather TEXT,
    staff TEXT,
    district TEXT,
    is_code INTEGER,
    count_hours TEXT,
    hour_start INTEGER,
    approach TEXT,
    left_turn INTEGER,
    through INTEGER,
    right_turn INTEGER,
    total INTEGER
);

CREATE TABLE IF NOT EXISTS manual_ped_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pdf_id INTEGER REFERENCES pdfs,
    traffic_id INTEGER,
    hour_start INTEGER,
    crossing TEXT,
    pedestrians INTEGER,
    school_children INTEGER
);
"""


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Step 1: Index ArcGIS data
# ---------------------------------------------------------------------------

def step_index(conn):
    print("Loading layer 176 (intersections)...")
    with open(LAYER176_PATH) as f:
        data = json.load(f)
    features = data["features"]
    rows = []
    for feat in features:
        a = feat["attributes"]
        rows.append((a["TrafficID"], a.get("IntersectionID"), a.get("lat"), a.get("lon"), a.get("intersection")))
    conn.executemany(
        "INSERT OR IGNORE INTO intersections (traffic_id, intersection_id, lat, lon, intersection) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM intersections").fetchone()[0]
    print(f"  Intersections: {count}")

    print("Loading layer 177 (segments)...")
    with open(LAYER177_PATH) as f:
        data = json.load(f)
    features = data["features"]
    rows = []
    for feat in features:
        a = feat["attributes"]
        geom = feat.get("geometry")
        geom_json = json.dumps(geom) if geom else None
        rows.append((a["OBJECTID"], a.get("ASSETID"), a.get("ID"), a.get("Location"), geom_json))
    conn.executemany(
        "INSERT OR IGNORE INTO segments (object_id, asset_id, segment_id, location, geom_json) VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    print(f"  Segments: {count}")


# ---------------------------------------------------------------------------
# Step 2: Scrape PDF URLs
# ---------------------------------------------------------------------------

class PDFLinkParser(HTMLParser):
    """Extract PDF hrefs from LADOT report pages."""

    def __init__(self):
        super().__init__()
        self.pdf_urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            if href.lower().endswith(".pdf"):
                self.pdf_urls.append(href)


async def scrape_one(session, traffic_id, sem):
    """Fetch one report page and return list of (traffic_id, url, filename, type)."""
    async with sem:
        url = REPORT_URL.format(traffic_id)
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()
        except Exception:
            return []

    parser = PDFLinkParser()
    parser.feed(html)

    results = []
    for pdf_url in parser.pdf_urls:
        # Normalize URL
        if not pdf_url.startswith("http"):
            pdf_url = "https://navigatela.lacity.org" + (pdf_url if pdf_url.startswith("/") else "/" + pdf_url)
        filename = pdf_url.rsplit("/", 1)[-1]
        if "/automatic_counts/" in pdf_url:
            pdf_type = "automatic"
        elif "/manual_counts/" in pdf_url:
            pdf_type = "manual"
        else:
            pdf_type = "unknown"
        results.append((traffic_id, pdf_url, filename, pdf_type))
    return results


async def step_scrape(conn, limit=None):
    traffic_ids = [row[0] for row in conn.execute("SELECT traffic_id FROM intersections ORDER BY traffic_id").fetchall()]
    if limit:
        traffic_ids = traffic_ids[:limit]

    # Skip already-scraped IDs
    scraped = set(
        row[0] for row in conn.execute("SELECT DISTINCT traffic_id FROM pdfs").fetchall()
    )
    todo = [tid for tid in traffic_ids if tid not in scraped]
    print(f"Scraping PDF URLs for {len(todo)} intersections ({len(scraped)} already done)...")
    if not todo:
        print("  Nothing to scrape.")
        return

    sem = asyncio.Semaphore(SCRAPE_CONCURRENCY)
    all_results = []

    async with aiohttp.ClientSession() as session:
        tasks = [scrape_one(session, tid, sem) for tid in todo]
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Scraping"):
            result = await coro
            all_results.extend(result)

    # Batch insert
    conn.executemany(
        "INSERT OR IGNORE INTO pdfs (traffic_id, url, filename, type) VALUES (?,?,?,?)",
        all_results,
    )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM pdfs").fetchone()[0]
    print(f"  Total PDFs in database: {total}")


# ---------------------------------------------------------------------------
# Step 3: Download PDFs
# ---------------------------------------------------------------------------

def sanitize_url(url):
    """Clean and properly encode a URL for downloading.

    - Strips soft-hyphen characters (U+00AD) that cause 404s
    - Percent-encodes special characters in the path (spaces, &, etc.)
    """
    # Remove soft-hyphens (invisible in HTML but break server-side lookups)
    url = url.replace("\u00ad", "")
    # Parse, encode path component, reassemble
    parts = urlparse(url)
    encoded_path = quote(parts.path, safe="/")
    return f"{parts.scheme}://{parts.netloc}{encoded_path}"


def safe_filename(filename):
    """Return a filesystem-safe filename, stripping soft-hyphens."""
    return filename.replace("\u00ad", "")


async def download_one(session, row, sem, error_counts):
    """Download a single PDF with retries. row = (id, url, filename, type)."""
    pdf_id, raw_url, raw_filename, pdf_type = row
    dest_dir = PDF_AUTO_DIR if pdf_type == "automatic" else PDF_MANUAL_DIR
    filename = safe_filename(raw_filename)
    dest = dest_dir / filename
    if dest.exists() and dest.stat().st_size > 0:
        return pdf_id, True

    url = sanitize_url(raw_url)
    last_error = None

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        async with sem:
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=90)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if len(data) < 100:
                            last_error = f"Suspiciously small response ({len(data)} bytes)"
                            log.warning("PDF %d: %s (attempt %d) - %s", pdf_id, filename, attempt, last_error)
                            continue
                        dest.write_bytes(data)
                        return pdf_id, True
                    elif resp.status == 404:
                        # Permanent failure, don't retry
                        log.debug("PDF %d: 404 Not Found - %s", pdf_id, url)
                        error_counts["404"] = error_counts.get("404", 0) + 1
                        return pdf_id, False
                    elif resp.status == 429:
                        # Rate limited - wait longer and retry
                        wait = DOWNLOAD_RETRY_DELAY * attempt * 3
                        log.warning("PDF %d: 429 rate limited, waiting %ds", pdf_id, wait)
                        await asyncio.sleep(wait)
                        last_error = f"HTTP 429"
                        continue
                    else:
                        last_error = f"HTTP {resp.status}"
                        log.warning("PDF %d: %s (attempt %d) - %s", pdf_id, filename, attempt, last_error)
            except asyncio.TimeoutError:
                last_error = "Timeout"
                log.debug("PDF %d: timeout (attempt %d) - %s", pdf_id, attempt, filename)
            except aiohttp.ClientError as e:
                last_error = f"{type(e).__name__}: {e}"
                log.debug("PDF %d: %s (attempt %d) - %s", pdf_id, last_error, attempt, filename)
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                log.warning("PDF %d: unexpected error (attempt %d) - %s: %s", pdf_id, attempt, filename, last_error)

        # Wait before retry
        if attempt < DOWNLOAD_RETRIES:
            await asyncio.sleep(DOWNLOAD_RETRY_DELAY * attempt)

    key = last_error.split(":")[0] if last_error else "unknown"
    error_counts[key] = error_counts.get(key, 0) + 1
    return pdf_id, False


async def step_download(conn, limit=None):
    PDF_AUTO_DIR.mkdir(parents=True, exist_ok=True)
    PDF_MANUAL_DIR.mkdir(parents=True, exist_ok=True)

    query = "SELECT id, url, filename, type FROM pdfs WHERE downloaded = 0"
    if limit:
        query += f" LIMIT {limit}"
    rows = conn.execute(query).fetchall()
    print(f"Downloading {len(rows)} PDFs...")
    if not rows:
        print("  Nothing to download.")
        return

    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    error_counts = {}
    success = 0
    failed = 0

    # Use a connector with connection pooling and keepalive
    connector = aiohttp.TCPConnector(limit=DOWNLOAD_CONCURRENCY, keepalive_timeout=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [download_one(session, row, sem, error_counts) for row in rows]
        for coro in tqdm(asyncio.as_completed(tasks), total=len(tasks), desc="Downloading"):
            pdf_id, ok = await coro
            if ok:
                conn.execute("UPDATE pdfs SET downloaded = 1 WHERE id = ?", (pdf_id,))
                success += 1
            else:
                failed += 1
            # Commit in batches to avoid losing progress on interrupt
            if (success + failed) % 200 == 0:
                conn.commit()

    conn.commit()
    print(f"  Downloaded: {success}/{len(rows)} (failed: {failed})")
    if error_counts:
        print(f"  Error breakdown: {error_counts}")


# ---------------------------------------------------------------------------
# Step 4: Parse automatic count PDFs
# ---------------------------------------------------------------------------

HOUR_MAP = {
    "12 AM": 0, "1 AM": 1, "2 AM": 2, "3 AM": 3, "4 AM": 4, "5 AM": 5,
    "6 AM": 6, "7 AM": 7, "8 AM": 8, "9 AM": 9, "10 AM": 10, "11 AM": 11,
    "12 NN": 12, "12 PM": 12, "1 PM": 13, "2 PM": 14, "3 PM": 15,
    "4 PM": 16, "5 PM": 17, "6 PM": 18, "7 PM": 19, "8 PM": 20,
    "9 PM": 21, "10 PM": 22, "11 PM": 23,
}

# Regex for header fields
RE_COUNTER = re.compile(r"Counter\s+(.+)", re.IGNORECASE)
RE_DATE = re.compile(r"Date\s+(\d{2}/\d{2}/\d{2,4})", re.IGNORECASE)
RE_LOCATION = re.compile(r"Location\s+(.+?)\s+Day of Week", re.IGNORECASE)
RE_DAY = re.compile(r"Day of Week\s+(\S+)", re.IGNORECASE)
RE_DIRECTION = re.compile(r"Direction\s+(.+?)\s+DOT District", re.IGNORECASE)
RE_DISTRICT = re.compile(r"DOT District\s+(\S+)", re.IGNORECASE)
RE_WEATHER = re.compile(r"Weather\s+(\S+)", re.IGNORECASE)
RE_SERIAL = re.compile(r"Serial Number\s+(\S+)", re.IGNORECASE)

# Regex for LADOT hourly row: "12 AM  19 20 26 25 90  34 37 20 36 127  217"
RE_HOUR_ROW = re.compile(
    r"^(1[0-2]|[1-9])\s+(AM|PM|NN)\s+"
    r"(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+"
    r"(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+"
    r"(\d[\d,]*)",
    re.MULTILINE,
)


def parse_int(s):
    """Parse integer, stripping commas."""
    return int(s.replace(",", ""))


MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "arpil": 4,  # known typo in source data
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

DAY_NORMALIZE = {
    "mon": "Monday", "monday": "Monday",
    "tue": "Tuesday", "tues": "Tuesday", "tuesday": "Tuesday", "tueday": "Tuesday",
    "wed": "Wednesday", "wednesday": "Wednesday",
    "thu": "Thursday", "thur": "Thursday", "thurs": "Thursday",
    "thursday": "Thursday", "thrusday": "Thursday",
    "fr": "Friday", "fri": "Friday", "friday": "Friday",
    "sat": "Saturday", "saturday": "Saturday",
    "sun": "Sunday", "sunday": "Sunday",
}


def normalize_day(raw):
    """Normalize day-of-week string."""
    if not raw:
        return None
    return DAY_NORMALIZE.get(raw.strip().lower(), raw.strip())


def parse_date_mdy(raw):
    """Parse M/D/Y date string to ISO format."""
    if not raw:
        return None
    parts = raw.replace("-", "/").split("/")
    if len(parts) == 3:
        mm, dd, yy = parts
        mm, dd = mm.strip(), dd.strip()
        yy = yy.strip()
        if len(yy) == 2:
            yy = "20" + yy if int(yy) < 50 else "19" + yy
        return f"{yy}-{mm.zfill(2)}-{dd.zfill(2)}"
    return raw


def normalize_date(raw):
    """Normalize various date formats to YYYY-MM-DD.

    Handles: 'APRIL 22, 1994', 'MAR 13, 1996', '4-30-1996', '4/30/96',
    'SEPT, 26, 2005', 'October 14 2010', 'February 28, 2017', '09/27/2018',
    '2017-3-28', 'Thursday, April 04, 2013', '8-Nov-06', 'FEB. 14, 2006',
    'OCT,04,2005', etc.
    """
    if not raw:
        return None
    raw = raw.strip().rstrip(".")

    # Already ISO format
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    # Strip leading day name: "Thursday, April 04, 2013" → "April 04, 2013"
    raw = re.sub(r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s+", "", raw, flags=re.IGNORECASE)

    # Strip ordinal suffixes: "14th" → "14", "1st" → "1"
    raw = re.sub(r"(\d+)(?:st|nd|rd|th)\b", r"\1", raw, flags=re.IGNORECASE)

    # Strip period from abbreviated months: "FEB." → "FEB", "JAN." → "JAN"
    raw = re.sub(r"\b([A-Za-z]{3})\.\s", r"\1 ", raw)

    # M/D/Y or M-D-Y numeric
    m = re.match(r"^(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})$", raw)
    if m:
        return parse_date_mdy(raw)

    # D-Mon-YY: "8-Nov-06" → "2006-11-08"
    m = re.match(r"^(\d{1,2})-([A-Za-z]+)-(\d{2,4})$", raw)
    if m:
        mm = MONTH_MAP.get(m.group(2).lower())
        if mm:
            yy = m.group(3)
            if len(yy) == 2:
                yy = "20" + yy if int(yy) < 50 else "19" + yy
            return f"{yy}-{mm:02d}-{int(m.group(1)):02d}"

    # "Month DD, YYYY" or "Month DD YYYY" or "Month, DD, YYYY" or "Month DD., YYYY"
    m = re.match(r"([A-Za-z]+),?\s+(\d{1,2})[.,]?\s*,?\s*(\d{4})", raw)
    if m:
        month_str = m.group(1).lower()
        mm = MONTH_MAP.get(month_str)
        if mm:
            return f"{m.group(3)}-{mm:02d}-{int(m.group(2)):02d}"

    return raw


def clean_street_name(raw):
    """Strip PDF form artifacts from a street name.

    Removes trailing '(Rev Apr 92)' version markers and 'I/S # N' intersection
    markers that appear on older LADOT form templates.
    """
    if not raw:
        return None
    raw = re.sub(r"\s*\(Rev\b[^)]*\)", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*I/S\s*#.*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*\bCOUNT\s+\d+\b.*", "", raw, flags=re.IGNORECASE)
    return raw.strip() or None


def parse_auto_pdf_ladot(text):
    """Parse classic LADOT automatic count format."""
    meta = {}
    m = RE_COUNTER.search(text)
    meta["counter"] = m.group(1).strip() if m else None
    m = RE_DATE.search(text)
    meta["date"] = parse_date_mdy(m.group(1)) if m else None
    m = RE_LOCATION.search(text)
    meta["location"] = m.group(1).strip() if m else None
    m = RE_DAY.search(text)
    meta["day_of_week"] = normalize_day(m.group(1).strip()) if m else None
    m = RE_DIRECTION.search(text)
    meta["sensor_layout"] = m.group(1).strip() if m else None
    m = RE_DISTRICT.search(text)
    meta["dot_district"] = m.group(1).strip() if m else None
    m = RE_WEATHER.search(text)
    meta["weather"] = m.group(1).strip() if m else None

    rows = []
    for m in RE_HOUR_ROW.finditer(text):
        hour_label = f"{m.group(1)} {m.group(2)}"
        hour = HOUR_MAP.get(hour_label)
        if hour is None:
            continue
        nw = [parse_int(m.group(i)) for i in range(3, 8)]
        se = [parse_int(m.group(i)) for i in range(8, 13)]
        total = parse_int(m.group(13))
        rows.append({
            "hour": hour,
            "nw_q1": nw[0], "nw_q2": nw[1], "nw_q3": nw[2], "nw_q4": nw[3], "nw_total": nw[4],
            "se_q1": se[0], "se_q2": se[1], "se_q3": se[2], "se_q4": se[3], "se_total": se[4],
            "total": total,
        })
    return meta, rows


def parse_auto_pdf_raw_volume(text):
    """Parse [Raw] Volume Report format (ADA/traffic counter devices)."""
    meta = {}
    m = re.search(r"Location:\s*(.+?)(?:\s+Raw Count|\s*$)", text, re.IGNORECASE)
    meta["location"] = m.group(1).strip() if m else None
    m = re.search(r"Begin:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, re.IGNORECASE)
    meta["date"] = parse_date_mdy(m.group(1)) if m else None
    m = re.search(r"Lane:\s*(\S+)", text, re.IGNORECASE)
    meta["sensor_layout"] = m.group(1).strip() if m else None
    m = re.search(r"Device ID:\s*(\S+)", text, re.IGNORECASE)
    meta["counter"] = m.group(1).strip() if m else None
    meta["dot_district"] = None
    meta["weather"] = None
    # Day from date line like "Tue,12/05/2017"
    m = re.search(r"(\w{3}),\d{1,2}/\d{1,2}/\d{2,4}", text)
    meta["day_of_week"] = normalize_day(m.group(1)) if m else None

    # Parse quarter-hour rows: "[HH:MM-HH:MM] volume speed temp ..."
    quarter_data = {}
    for m in re.finditer(
        r"\[(\d{2}):(\d{2})-\d{2}:\d{2}\]\s+(\d[\d,]*)", text
    ):
        hour = int(m.group(1))
        volume = parse_int(m.group(3))
        quarter_data.setdefault(hour, 0)
        quarter_data[hour] += volume

    # Build hourly rows (single direction only, stored in nw_total)
    rows = []
    for hour in sorted(quarter_data):
        total = quarter_data[hour]
        rows.append({
            "hour": hour,
            "nw_q1": 0, "nw_q2": 0, "nw_q3": 0, "nw_q4": 0,
            "nw_total": total,
            "se_q1": 0, "se_q2": 0, "se_q3": 0, "se_q4": 0,
            "se_total": 0,
            "total": total,
        })
    return meta, rows


def parse_auto_pdf_nds(text):
    """Parse NDS automatic count format (24h clock, hourly summary columns)."""
    meta = {}
    m = re.search(r"Date:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, re.IGNORECASE)
    meta["date"] = parse_date_mdy(m.group(1)) if m else None
    m = re.search(r"Day:\s*(\S+)", text, re.IGNORECASE)
    meta["day_of_week"] = normalize_day(m.group(1).strip()) if m else None
    # Location is typically the line after "VOLUME"
    m = re.search(r"VOLUME\s*\n(.+)", text, re.IGNORECASE)
    meta["location"] = m.group(1).strip() if m else None
    m = re.search(r"Project\s*#:\s*(\S+)", text, re.IGNORECASE)
    meta["counter"] = m.group(1).strip() if m else None
    meta["sensor_layout"] = None
    meta["dot_district"] = None
    meta["weather"] = None

    # Try several NDS sub-formats:

    # Format A: Right-side hourly summary "00:00 01:00  NB  SB  EB  WB  Total" (4-dir)
    rows = []
    for m in re.finditer(
        r"(\d{2}):00\s+\d{2}:00\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)",
        text,
    ):
        hour = int(m.group(1))
        nb = parse_int(m.group(2))
        sb = parse_int(m.group(3))
        eb = parse_int(m.group(4))
        wb = parse_int(m.group(5))
        total = parse_int(m.group(6))
        rows.append({
            "hour": hour,
            "nw_q1": 0, "nw_q2": 0, "nw_q3": 0, "nw_q4": 0, "nw_total": nb + wb,
            "se_q1": 0, "se_q2": 0, "se_q3": 0, "se_q4": 0, "se_total": sb + eb,
            "total": total,
        })

    # Format B: Right-side hourly summary "00:00 01:00  dir1  dir2  Total" (2-dir)
    if not rows:
        for m in re.finditer(r"(\d{2}):00\s+\d{2}:00\s+(\d[\d,]*)\s+(\d[\d,]*)\s+(\d[\d,]*)", text):
            hour = int(m.group(1))
            nw_total = parse_int(m.group(2))
            se_total = parse_int(m.group(3))
            total = parse_int(m.group(4))
            rows.append({
                "hour": hour,
                "nw_q1": 0, "nw_q2": 0, "nw_q3": 0, "nw_q4": 0, "nw_total": nw_total,
                "se_q1": 0, "se_q2": 0, "se_q3": 0, "se_q4": 0, "se_total": se_total,
                "total": total,
            })

    # Format C: AM/PM side-by-side with :45 lines containing hourly totals.
    # The :45 lines interleave q4 and hourly values per direction:
    #   4-dir (10 vals): q4_NB hrly_NB q4_SB hrly_SB q4_EB hrly_EB q4_WB hrly_WB q4_T hrly_T
    #   2-dir (8 vals):  q4_d1 hrly_d1 q4_d2 hrly_d2 0 0 q4_T hrly_T
    #   2-dir (6 vals):  q4_d1 hrly_d1 q4_d2 hrly_d2 q4_T hrly_T
    # Each AM :45 segment is followed by PM :45 on the same line.
    if not rows:
        for m in re.finditer(r"(\d{1,2}):45\s+((?:\d[\d,]*(?:\s+|$))+)", text):
            hour = int(m.group(1))
            vals = [parse_int(v) for v in m.group(2).strip().split()]
            if len(vals) < 6:
                continue
            # Hourly total is always the last value; dir hourlys at odd indices
            hrly_total = vals[-1]
            if len(vals) >= 10:
                # 4-dir: hrly_NB=vals[1], hrly_SB=vals[3], hrly_EB=vals[5], hrly_WB=vals[7]
                nw_total = vals[1] + vals[7]
                se_total = vals[3] + vals[5]
            elif len(vals) >= 6:
                # 2-dir (with or without zero-direction placeholders)
                nw_total = vals[1]
                se_total = vals[3]
            rows.append({
                "hour": hour,
                "nw_q1": 0, "nw_q2": 0, "nw_q3": 0, "nw_q4": 0,
                "nw_total": nw_total,
                "se_q1": 0, "se_q2": 0, "se_q3": 0, "se_q4": 0,
                "se_total": se_total,
                "total": hrly_total,
            })

    return meta, rows


def parse_auto_pdf_wiltec(text):
    """Parse WILTEC 24-HOUR ADT COUNT SUMMARY format.

    Each page has one day's data with two direction columns (NB/SB or EB/WB).
    For multi-day PDFs, only the first day is returned (one PDF = one db record).
    """
    meta = {}
    # Location may span two lines: "LOCATION: AVALON BOULEVARD SOUTH OF\n88TH PLACE"
    m = re.search(r"LOCATION:\s*(.+?)(?=\nDATE:)", text, re.IGNORECASE | re.DOTALL)
    if m:
        meta["location"] = " ".join(m.group(1).split())
    else:
        meta["location"] = None
    # DATE line: "DATE: WEDNESDAY NOVEMBER 9, 2016"
    m = re.search(r"DATE:\s*(?:\w+day\s+)?(\w+\s+\d{1,2},?\s+\d{4})", text, re.IGNORECASE)
    if m:
        meta["date"] = normalize_date(m.group(1))
    else:
        m = re.search(r"DATE:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, re.IGNORECASE)
        meta["date"] = parse_date_mdy(m.group(1)) if m else None
    # Day of week from DATE line
    m = re.search(r"DATE:\s*(\w+day)", text, re.IGNORECASE)
    meta["day_of_week"] = normalize_day(m.group(1)) if m else None
    dirs = re.findall(r"DIRECTION:\s*(\w+)", text)
    meta["sensor_layout"] = "/".join(dirs[:2]) if dirs else None
    meta["counter"] = "WILTEC"
    meta["dot_district"] = None
    meta["weather"] = None

    # Parse hourly rows.  WILTEC has two side-by-side direction tables:
    #   H:MM q1 q2 q3 q4 HOUR   H:MM q1 q2 q3 q4 HOUR
    rows = []
    for m in re.finditer(
        r"^(\d{1,2}):00\s+"
        r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+"
        r"\d{1,2}:00\s+"
        r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
        text, re.MULTILINE,
    ):
        hour = int(m.group(1))
        nw_q1, nw_q2, nw_q3, nw_q4, nw_total = [int(m.group(i)) for i in range(2, 7)]
        se_q1, se_q2, se_q3, se_q4, se_total = [int(m.group(i)) for i in range(7, 12)]
        rows.append({
            "hour": hour,
            "nw_q1": nw_q1, "nw_q2": nw_q2, "nw_q3": nw_q3, "nw_q4": nw_q4,
            "nw_total": nw_total,
            "se_q1": se_q1, "se_q2": se_q2, "se_q3": se_q3, "se_q4": se_q4,
            "se_total": se_total,
            "total": nw_total + se_total,
        })
        # Only take first 24 rows (first day) for multi-day PDFs
        if len(rows) >= 24:
            break
    return meta, rows


def parse_auto_pdf_aimtd(text):
    """Parse AimTD LLC automatic count format (quarter-hour AM/PM side-by-side)."""
    meta = {}
    m = re.search(r"ADT\w*\s+(.+?)\.?\s*Prepared by AimTD", text)
    meta["location"] = m.group(1).strip() if m else None
    m = re.search(r"(\w+day),?\s+(\w+\s+\d{1,2},?\s+\d{4})", text)
    if m:
        meta["day_of_week"] = normalize_day(m.group(1))
        meta["date"] = normalize_date(m.group(2))
    else:
        meta["day_of_week"] = None
        meta["date"] = None
    m = re.search(r"PROJECT:\s*(\S+)", text, re.IGNORECASE)
    meta["counter"] = m.group(1).strip() if m else "AimTD"
    m = re.search(r"((?:NB|SB|EB|WB)\s+APP)", text)
    meta["sensor_layout"] = m.group(1).strip() if m else None
    meta["dot_district"] = None
    meta["weather"] = None

    # AimTD has :45 lines with hourly totals: "H:45  q4_val  hourly_total  hourly_total"
    # The AM and PM periods are side-by-side:
    #   AM Period  DIR APP   PM Period  DIR APP
    #   0:00 15            12:00 74
    #   ...
    #   0:45 9 42 42       12:45 72 266 266
    # We extract from the :45 lines which have the hourly totals.
    rows = []
    for m in re.finditer(
        r"^(\d{1,2}):45\s+\d+\s+(\d+)\s+(\d+)",
        text, re.MULTILINE,
    ):
        hour = int(m.group(1))
        hourly_total = parse_int(m.group(2))
        rows.append({
            "hour": hour,
            "nw_q1": 0, "nw_q2": 0, "nw_q3": 0, "nw_q4": 0,
            "nw_total": hourly_total,
            "se_q1": 0, "se_q2": 0, "se_q3": 0, "se_q4": 0,
            "se_total": 0,
            "total": hourly_total,
        })
    return meta, rows


def parse_auto_pdf(filepath):
    """Parse an automatic count PDF, return (meta, rows) or raise."""
    with pdfplumber.open(filepath) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Clean up (cid:X) character-ID placeholders → spaces
    if "(cid:" in text:
        text = re.sub(r"\(cid:\d+\)", " ", text)

    if not text.strip():
        # Try OCR fallback for scanned image PDFs
        try:
            import pypdfium2 as pdfium
            import pytesseract
            from PIL import Image
            doc = pdfium.PdfDocument(filepath)
            ocr_pages = []
            for i in range(len(doc)):
                page = doc[i]
                bmp = page.render(scale=2)
                img = bmp.to_pil()
                ocr_pages.append(pytesseract.image_to_string(img))
            text = "\n".join(ocr_pages)
        except Exception:
            pass
        if not text.strip():
            raise ValueError("Empty text extraction")

    # Speed studies are not traffic counts
    if re.search(r"Speed Study Report|SPEED STUDY", text, re.IGNORECASE):
        raise ValueError("Speed study (not traffic count)")

    # Some PDFs in the automatic URL path are actually manual counts
    if re.search(r"TRAFFIC COUNT SUMMARY|MANUAL TRAFFIC COUNT", text, re.IGNORECASE):
        raise ValueError("Misclassified: manual count PDF in automatic path")

    # WILTEC 24-HOUR ADT COUNT SUMMARY
    if "WILTEC" in text:
        meta, rows = parse_auto_pdf_wiltec(text)
        if len(rows) >= 12:
            return meta, rows

    # AimTD LLC format
    if "AimTD" in text:
        meta, rows = parse_auto_pdf_aimtd(text)
        if len(rows) >= 12:
            return meta, rows

    # [Raw] Volume Report format (ADA/traffic counter devices)
    if re.search(r"\[Raw\]\s*Volume Report", text, re.IGNORECASE):
        meta, rows = parse_auto_pdf_raw_volume(text)
        if len(rows) >= 12:
            return meta, rows

    # Detect format: NDS uses "Prepared by" + 24h clock, LADOT uses "24 Hours Traffic Volume"
    if re.search(r"Prepared by.*(?:NDS|National Data|ATD)", text, re.IGNORECASE):
        meta, rows = parse_auto_pdf_nds(text)
    else:
        meta, rows = parse_auto_pdf_ladot(text)

    if len(rows) < 12:
        raise ValueError(f"Only {len(rows)} hour rows parsed (expected ~24)")

    return meta, rows


def step_parse_auto(conn):
    rows = conn.execute(
        "SELECT id, traffic_id, filename FROM pdfs WHERE type='automatic' AND downloaded=1 AND parsed=0"
    ).fetchall()
    print(f"Parsing {len(rows)} automatic count PDFs...")

    success = 0
    errors = 0
    for pdf_id, traffic_id, filename in tqdm(rows, desc="Parsing auto"):
        filepath = PDF_AUTO_DIR / filename
        if not filepath.exists():
            conn.execute("UPDATE pdfs SET parse_error = 'file missing' WHERE id = ?", (pdf_id,))
            errors += 1
            continue
        try:
            meta, hour_rows = parse_auto_pdf(filepath)
        except ValueError as e:
            if str(e).startswith("Misclassified"):
                # PDF is in the automatic folder but contains manual count data.
                # Re-parse with the manual parser and store in manual tables.
                try:
                    meta, counts, peds = parse_manual_pdf(filepath)
                except Exception as e2:
                    conn.execute("UPDATE pdfs SET parse_error = ? WHERE id = ?", (str(e2)[:500], pdf_id))
                    errors += 1
                    continue
                if not counts:
                    conn.execute("UPDATE pdfs SET parse_error = 'misclassified: no manual count rows parsed' WHERE id = ?", (pdf_id,))
                    errors += 1
                    continue
                for c in counts:
                    conn.execute(
                        """INSERT INTO manual_counts
                        (pdf_id, traffic_id, ns_street, ew_street, date, day_of_week,
                         weather, staff, district, is_code, count_hours,
                         hour_start, approach, left_turn, through, right_turn, total)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            pdf_id, traffic_id, meta.get("ns_street"), meta.get("ew_street"),
                            meta.get("date"), meta.get("day_of_week"), meta.get("weather"),
                            meta.get("staff"), meta.get("district"), meta.get("is_code"),
                            meta.get("count_hours"),
                            c["hour_start"], c["approach"],
                            c["left_turn"], c["through"], c["right_turn"], c["total"],
                        ),
                    )
                for p in peds:
                    conn.execute(
                        """INSERT INTO manual_ped_counts
                        (pdf_id, traffic_id, hour_start, crossing, pedestrians, school_children)
                        VALUES (?,?,?,?,?,?)""",
                        (pdf_id, traffic_id, p["hour_start"], p["crossing"],
                         p["pedestrians"], p["school_children"]),
                    )
                conn.execute("UPDATE pdfs SET parsed = 1 WHERE id = ?", (pdf_id,))
                success += 1
            else:
                conn.execute("UPDATE pdfs SET parse_error = ? WHERE id = ?", (str(e)[:500], pdf_id))
                errors += 1
            continue
        except Exception as e:
            conn.execute("UPDATE pdfs SET parse_error = ? WHERE id = ?", (str(e)[:500], pdf_id))
            errors += 1
            continue

        for hr in hour_rows:
            conn.execute(
                """INSERT INTO automatic_counts
                (pdf_id, traffic_id, location, date, day_of_week, weather,
                 counter, sensor_layout, dot_district, hour,
                 nw_q1, nw_q2, nw_q3, nw_q4, nw_total,
                 se_q1, se_q2, se_q3, se_q4, se_total, total)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pdf_id, traffic_id, meta["location"], meta["date"],
                    meta["day_of_week"], meta["weather"], meta["counter"],
                    meta["sensor_layout"], meta["dot_district"], hr["hour"],
                    hr["nw_q1"], hr["nw_q2"], hr["nw_q3"], hr["nw_q4"], hr["nw_total"],
                    hr["se_q1"], hr["se_q2"], hr["se_q3"], hr["se_q4"], hr["se_total"],
                    hr["total"],
                ),
            )
        conn.execute("UPDATE pdfs SET parsed = 1 WHERE id = ?", (pdf_id,))
        success += 1

        if (success + errors) % 100 == 0:
            conn.commit()

    conn.commit()
    print(f"  Parsed: {success}, Errors: {errors}")


# ---------------------------------------------------------------------------
# Step 5: Parse manual count PDFs
# ---------------------------------------------------------------------------

def ocr_pdf(filepath, dpi_scale=4):
    """Render PDF pages to images and OCR them. Returns list of page texts."""
    import pypdfium2 as pdfium
    import pytesseract

    pdf = pdfium.PdfDocument(str(filepath))
    pages = []
    for i in range(len(pdf)):
        page = pdf[i]
        bitmap = page.render(scale=dpi_scale)
        img = bitmap.to_pil()
        text = pytesseract.image_to_string(img, config="--psm 6")
        pages.append(text)
    return pages


# ---------------------------------------------------------------------------
# OCR-aware helpers for scanned manual PDFs
# ---------------------------------------------------------------------------

def clean_ocr_line(line):
    """Strip OCR table-border artifacts and fix common letter/digit confusions.

    Handles:
    - Table cell borders: | [ ] { } ( ) _ \\
    - Digit/letter OCR confusions in numeric contexts: 'i'/'I' -> '1', isolated 'o' -> '0'
    - Dot-separated hour ranges: '8.9' -> '8-9'
    """
    # Normalize hour separator X.Y -> X-Y (e.g. "8.9" -> "8-9") before stripping dots
    line = re.sub(r'\b(\d{1,2})\.(\d{1,2})\b', r'\1-\2', line)
    # Remove table border chars / form-field markers
    cleaned = re.sub(r'[|{}\[\]()_,\\+~]', ' ', line)
    # Fix 'i'/'I' adjacent to digits (OCR reading '1' as 'i'):
    # e.g. "24ii" -> "2411", "i319" -> "1319"
    cleaned = re.sub(
        r'\b\d+[iIlL]+\d*\b|\b[iIlL]+\d+\b',
        lambda m: re.sub(r'[iIlL]', '1', m.group()),
        cleaned,
    )
    # Isolated 'o' / 'O' -> 0
    cleaned = re.sub(r'(?<!\w)o(?!\w)', '0', cleaned, flags=re.IGNORECASE)
    # Strip single trailing non-digit letter after digit sequence (OCR noise like "75t")
    cleaned = re.sub(r'(\b\d+)[a-hj-np-zA-HJ-NP-Z]+\b', r'\1', cleaned)
    # Collapse whitespace
    return re.sub(r'\s+', ' ', cleaned).strip()


MAX_APPROACH_VOL = 4999  # Any single movement volume above this is suspect
_TOTAL_TOLERANCE = 1.25  # Total may exceed computed sum by up to 25% (rounding/OCR)


def _parse_approach_nums(nums):
    """Given numbers for a single turning-movement approach, return (lt, th, rt, total).

    Handles rows where OCR dropped the left-turn column when it was 0 or small.
    Rejects implausibly large values (merged OCR digits) and validates that the
    Total column is consistent with the movement sub-totals.

    Returns (0, 0, 0, 0) when the numbers cannot be validated.
    """
    # Filter out obviously merged / corrupt numbers
    nums = [n for n in nums if n <= MAX_APPROACH_VOL]

    if len(nums) >= 4:
        lt, th, rt, total = nums[0], nums[1], nums[2], nums[3]
        computed = lt + th + rt
        # Validate: total must be close to computed (within tolerance)
        if computed > 0 and total >= computed * 0.75 and total <= computed * _TOTAL_TOLERANCE:
            return lt, th, rt, total
        # 4-number validation failed — try 3-number (no Lt) interpretation
        if len(nums) >= 3:
            th2, rt2, total2 = nums[0], nums[1], nums[2]
            computed2 = th2 + rt2
            lt2 = max(0, total2 - computed2)
            if computed2 > 0 and total2 >= computed2 * 0.75 and total2 <= computed2 * _TOTAL_TOLERANCE:
                return lt2, th2, rt2, total2
        # Both failed — might be a total-only row
        if len(nums) == 1:
            return 0, 0, 0, nums[0]
        return 0, 0, 0, 0

    elif len(nums) == 3:
        # Lt column dropped by OCR; infer from Total - Th - Rt
        th, rt, total = nums[0], nums[1], nums[2]
        computed = th + rt
        lt = max(0, total - computed)
        # Basic sanity: computed should not wildly exceed total
        if computed > total * _TOTAL_TOLERANCE and computed > 50:
            # Might be: th=total and rt is extra, or numbers are shuffled
            return 0, 0, 0, 0
        return lt, th, rt, total

    elif len(nums) == 2:
        # Two numbers: likely (th, total) with Lt and Rt dropped by OCR.
        # Reject if through > total: that means these are not (th, total) pairs.
        th, total = nums[0], nums[1]
        if th > total * 1.25 and th > 50:
            return 0, 0, 0, 0
        lt = rt = 0
        return lt, th, rt, total

    elif len(nums) == 1:
        total = nums[0]
        return 0, 0, 0, total

    return 0, 0, 0, 0


def parse_classic_manual_ocr(text):
    """Parse a classic LADOT manual count that has been through OCR and has artifacts.

    Handles:
    - | ] [ { } delimiters between table columns
    - Merged numbers (e.g. '1422367' for '142 2367')
    - Missing left-turn column when OCR drops it
    - Dot-separated hour ranges ('8.9' instead of '8-9')
    - Hour ranges like '15-16' (PM hours stored as 24h values > 12)
    """
    meta = {}
    m = RE_MANUAL_NS.search(text)
    meta["ns_street"] = clean_street_name(m.group(1)) if m else None
    m = RE_MANUAL_EW.search(text)
    meta["ew_street"] = clean_street_name(m.group(1)) if m else None
    m = RE_MANUAL_DATE.search(text)
    meta["date"] = normalize_date(m.group(1).strip()) if m else None
    m = RE_MANUAL_DAY.search(text)
    meta["day_of_week"] = normalize_day(m.group(1).strip()) if m else None
    m = RE_MANUAL_WEATHER.search(text)
    meta["weather"] = m.group(1).strip() if m else None
    m = RE_MANUAL_HOURS.search(text)
    meta["count_hours"] = m.group(1).strip() if m else None
    m = RE_MANUAL_DISTRICT.search(text)
    meta["district"] = m.group(1).strip() if m else None
    m = RE_MANUAL_ISCODE.search(text)
    meta["is_code"] = 1 if m else 0

    counts = []
    lines = text.split("\n")
    current_block = None

    for line in lines:
        if re.search(r"NORTHBOUND\s+Approach.*SOUTHBOUND\s+Approach", line, re.IGNORECASE):
            current_block = "NS"
            continue
        if re.search(r"EASTBOUND\s+Approach.*WESTBOUND\s+Approach", line, re.IGNORECASE):
            current_block = "EW"
            continue
        if current_block and re.match(r'\s*TOTAL\b', line, re.IGNORECASE):
            current_block = None
            continue

        if not current_block:
            continue

        # Check if line starts with a valid hour range
        cleaned = clean_ocr_line(line)
        m = re.match(r'^(\d{1,2})-(\d{1,2})\b\s*(.*)', cleaned)
        if not m:
            continue

        hr_start = int(m.group(1))
        rest = m.group(3)

        # Locate the second hour-range marker in the rest of the line
        m2 = re.search(r'\b(\d{1,2})-(\d{1,2})\b', rest)
        if m2:
            part_a_str = rest[:m2.start()]
            part_b_str = rest[m2.end():]
        else:
            part_a_str = rest
            part_b_str = ""

        nums_a = [int(x) for x in re.findall(r'\d+', part_a_str)]
        nums_b_all = [int(x) for x in re.findall(r'\d+', part_b_str)]

        # Approach B: take first 3-4 values that are plausible volumes
        # (remaining values are NS/EW total, ped counts, etc.)
        nums_b = []
        for n in nums_b_all:
            if n <= MAX_APPROACH_VOL:
                nums_b.append(n)
            if len(nums_b) == 4:
                break

        if current_block == "NS":
            approach_a, approach_b = "NB", "SB"
        else:
            approach_a, approach_b = "EB", "WB"

        lt_a, th_a, rt_a, total_a = _parse_approach_nums(nums_a)
        lt_b, th_b, rt_b, total_b = _parse_approach_nums(nums_b)

        if total_a > 0:
            counts.append({
                "hour_start": hr_start,
                "approach": approach_a,
                "left_turn": lt_a,
                "through": th_a,
                "right_turn": rt_a,
                "total": total_a,
            })
        if total_b > 0:
            counts.append({
                "hour_start": hr_start,
                "approach": approach_b,
                "left_turn": lt_b,
                "through": th_b,
                "right_turn": rt_b,
                "total": total_b,
            })

    return meta, counts, []


# ---------------------------------------------------------------------------
# Southland Car Counters turning movement format
# ---------------------------------------------------------------------------

# Movement column names in Southland / Traffic Solution table headers
_ALL_MOVEMENTS = ['NL', 'NT', 'NR', 'SL', 'ST', 'SR', 'EL', 'ET', 'ER', 'WL', 'WT', 'WR']

# Traffic Solution column order (SBRT SBTH SBLT WBRT WBTH WBLT NBRT NBTH NBLT EBRT EBTH EBLT)
# maps to standard movements:
_TS_COL_ORDER = ['SR', 'ST', 'SL', 'WR', 'WT', 'WL', 'NR', 'NT', 'NL', 'ER', 'ET', 'EL']


def parse_southland_manual(text, pages_text):
    """Parse Southland Car Counters intersection turning movement format.

    Format example::

        N-S STREET: ...  DATE: 6/27/2006  DAY: TUESDAY  PROJECT # 06-2275-006
        NORTHBOUND  SOUTHBOUND  EASTBOUND  WESTBOUND
        NL NT NR  SL ST SR  EL ET ER  WL WT WR  TOTAL
        LANES: 0 1 0  0 0 0  0 3 0  1 3 0
        7:00 AM  1  8  215  1  3  245  473
        ...
        TOTAL NL NT NR SL ST SR EL ET ER WL WT WR  TOTAL
        VOLUMES = 16 0 92 0 0 0 0 2330 16 30 2313 0 4797

    The VOLUMES row provides totals for all 12 movement columns.  Individual
    15-minute rows only include values for movements with non-zero VOLUMES
    totals (zeros are omitted), followed by the interval TOTAL.
    """
    meta = {}
    # Date and day
    m = re.search(r'DATE:\s*(\d{1,2}/\d{1,2}/\d{2,4})', text, re.IGNORECASE)
    meta["date"] = parse_date_mdy(m.group(1)) if m else None
    m = re.search(r'DAY:\s*(\S+)', text, re.IGNORECASE)
    meta["day_of_week"] = normalize_day(m.group(1)) if m else None
    # Street names
    m = re.search(r'N-?S\s+STREET:\s*(.+?)(?:\s+DATE:|\s*$)', text, re.IGNORECASE)
    meta["ns_street"] = m.group(1).strip() if m else None
    m = re.search(r'E-?W\s+STREET:\s*(.+?)(?:\s+DAY:|\s*$)', text, re.IGNORECASE)
    meta["ew_street"] = m.group(1).strip() if m else None
    m = re.search(r'PROJECT\s*#\s*(\S+)', text, re.IGNORECASE)
    meta["staff"] = m.group(1).strip() if m else None
    m = re.search(r'LOCATION:\s*(.+?)(?:\s*$)', text, re.IGNORECASE | re.MULTILINE)
    meta["district"] = m.group(1).strip() if m else None
    meta["weather"] = None
    meta["is_code"] = 0
    meta["count_hours"] = None

    # Gather data across all pages
    quarter_data = []

    for page_text in pages_text:
        # Skip "DUAL WHEELED VEHICLES" pages
        if re.search(r'DUAL\s+WHEELED\s+VEHICLES', page_text, re.IGNORECASE):
            continue

        # Get the non-zero movement columns from the VOLUMES row on this page.
        # OCR artifacts: leading garbage (":", "i"), merged numbers ("2912-28").
        m_vol = re.search(r'VOLUMES\s*=\s*([\d\s\-]+)', page_text, re.IGNORECASE)
        if not m_vol:
            continue
        raw_vol = m_vol.group(1)
        # Split merged numbers like "2912-28" into two separate values
        raw_vol = re.sub(r'(\d)-(\d)', r'\1 \2', raw_vol)
        vol_nums_str = re.findall(r'\d+', raw_vol)
        if len(vol_nums_str) < 12:
            continue
        try:
            vol_nums = list(map(int, vol_nums_str[:12]))
        except ValueError:
            continue
        totals = dict(zip(_ALL_MOVEMENTS, vol_nums))
        nz_cols = [mv for mv in _ALL_MOVEMENTS if totals.get(mv, 0) > 0]

        # Parse each 15-minute time row.
        # OCR artifacts: leading "|", "i", "j" before the time; trailing
        # garbage like "~«686" for "686".
        for line in page_text.split('\n'):
            # Strip leading non-digit characters (pipe, letter artifacts)
            stripped = re.sub(r'^[^0-9]+', '', line.strip())
            m_row = re.match(
                r'^(\d{1,2}):(\d{2})\s+(AM|PM)\s+(.*)',
                stripped,
                re.IGNORECASE,
            )
            if not m_row:
                continue
            hr = int(m_row.group(1))
            minute = int(m_row.group(2))
            ampm = m_row.group(3).upper()
            # Convert to 24h
            if ampm == 'PM' and hr != 12:
                hr += 12
            elif ampm == 'AM' and hr == 12:
                hr = 0

            # Extract all digit sequences from the rest of the line
            vals = [int(x) for x in re.findall(r'\d+', m_row.group(4))]

            # Expected: len(nz_cols) values + 1 TOTAL
            if len(vals) != len(nz_cols) + 1:
                continue

            row_total = vals[-1]
            row_dict = dict(zip(nz_cols, vals[:-1]))

            quarter_data.append({
                "hour": hr,
                "minute": minute,
                "NL": row_dict.get("NL", 0),
                "NT": row_dict.get("NT", 0),
                "NR": row_dict.get("NR", 0),
                "SL": row_dict.get("SL", 0),
                "ST": row_dict.get("ST", 0),
                "SR": row_dict.get("SR", 0),
                "EL": row_dict.get("EL", 0),
                "ET": row_dict.get("ET", 0),
                "ER": row_dict.get("ER", 0),
                "WL": row_dict.get("WL", 0),
                "WT": row_dict.get("WT", 0),
                "WR": row_dict.get("WR", 0),
            })

    # Aggregate quarters to hourly counts by approach
    hourly = {}
    for q in quarter_data:
        h = q["hour"]
        if h not in hourly:
            hourly[h] = {k: 0 for k in ["NL", "NT", "NR", "SL", "ST", "SR",
                                          "EL", "ET", "ER", "WL", "WT", "WR"]}
        for k in hourly[h]:
            hourly[h][k] += q[k]

    counts = []
    for h, d in sorted(hourly.items()):
        for approach, lt_k, th_k, rt_k in [
            ("NB", "NL", "NT", "NR"),
            ("SB", "SL", "ST", "SR"),
            ("EB", "EL", "ET", "ER"),
            ("WB", "WL", "WT", "WR"),
        ]:
            total = d[lt_k] + d[th_k] + d[rt_k]
            if total > 0:
                counts.append({
                    "hour_start": h,
                    "approach": approach,
                    "left_turn": d[lt_k],
                    "through": d[th_k],
                    "right_turn": d[rt_k],
                    "total": total,
                })

    return meta, counts, []


# ---------------------------------------------------------------------------
# "Traffic Solution" format (INTERSECTION TURNING MOVEMENT COUNT SUMMARY)
# ---------------------------------------------------------------------------

# Column order used by Traffic Solution reports:
#   SBRT SBTH SBLT WBRT WBTH WBLT NBRT NBTH NBLT EBRT EBTH EBLT
# mapped to: SR ST SL WR WT WL NR NT NL ER ET EL


def _clean_ts_values(line):
    """Clean OCR artifacts from value portion of a Traffic Solution data row.

    Handles:
    - Isolated letters that OCR reads as 0 (Q, o, ©, a, etc.)
    - Merged numbers with dash: '10-232' -> '10 232'
    - Trailing letters on numbers: '22Btt' -> '22'
    """
    # Replace zero-like OCR characters: single uppercase Q, lowercase o,
    # copyright sign ©, any other isolated letter between whitespace
    line = re.sub(r'(?<!\w)[QqoO©](?!\w)', '0', line)
    line = re.sub(r'(?<=\s)[a-zA-Z](?=\s)', '0', line)  # isolated letter -> 0
    line = re.sub(r'[\(\)\[\]«»]', ' ', line)            # brackets -> space
    # Merge-split: "10-232" -> "10 232" (dash between digits in value area)
    line = re.sub(r'(\d)-(\d)', r'\1 \2', line)
    # Trailing letter(s) after a digit sequence -> strip
    line = re.sub(r'(\b\d+)[a-zA-Z]+\b', r'\1', line)
    return line


def parse_traffic_solution_manual(text, pages_text):
    """Parse "The Traffic Solution" intersection turning movement count format.

    Each non-dual-wheeled page has 15-minute rows with exactly 12 movement
    values (SBRT SBTH SBLT WBRT WBTH WBLT NBRT NBTH NBLT EBRT EBTH EBLT)
    using a HHMM-HHMM time stamp.

    The format detection relies on finding the "SBRT | SBTH" column header on
    at least one page; all non-dual-wheeled pages in the same file are then
    assumed to use the same 12-column layout.
    """
    meta = {}
    m = re.search(r'DATE:\s*(\w+),?\s*(\w+)\s+(\d{1,2}),?\s*(\d{4})', text, re.IGNORECASE)
    if m:
        raw_date = f"{m.group(2)} {m.group(3)}, {m.group(4)}"
        meta["date"] = normalize_date(raw_date)
    else:
        m = re.search(r'DATE:\s*(.+)', text, re.IGNORECASE)
        meta["date"] = normalize_date(m.group(1).strip()) if m else None

    m = re.search(r'INTERSECTION\s+N/?S\s+(.+)', text, re.IGNORECASE)
    meta["ns_street"] = m.group(1).strip() if m else None
    m = re.search(r'(?:INTERSECTION\s+)?E/?W\s+(.+)', text, re.IGNORECASE)
    meta["ew_street"] = m.group(1).strip() if m else None
    m = re.search(r'PROJECT:\s*(.+)', text, re.IGNORECASE)
    meta["district"] = m.group(1).strip() if m else None
    # Day of week from "DATE: TUESDAY, AUGUST 17, 2010"
    m_day = re.search(r'DATE:\s*(\w+),', text, re.IGNORECASE)
    meta["day_of_week"] = normalize_day(m_day.group(1)) if m_day else None
    meta["weather"] = None
    meta["is_code"] = 0
    meta["count_hours"] = None
    meta["staff"] = None

    quarter_data = []

    for page_text in pages_text:
        # Skip dual-wheeled pages
        if re.search(r'DUAL\s+WHEELED\s+VEHICLES', page_text, re.IGNORECASE):
            continue

        for line in page_text.split('\n'):
            raw = line.strip()
            # Match HHMM-HHMM pattern; the time range must look like a 15-min interval
            m_row = re.match(r'^(\d{3,4})-(\d{3,4})\s+(.*)', raw)
            if not m_row:
                continue
            start_str = m_row.group(1)
            end_str = m_row.group(2)

            # Filter out 1-hour rolling totals (HHMM-HHMM spans > 15 min)
            # 15-min rows: end = start + 15 minutes
            try:
                if len(start_str) == 3:
                    s_hr, s_mi = int(start_str[0]), int(start_str[1:])
                else:
                    s_hr, s_mi = int(start_str[:2]), int(start_str[2:])
                if len(end_str) == 3:
                    e_hr, e_mi = int(end_str[0]), int(end_str[1:])
                else:
                    e_hr, e_mi = int(end_str[:2]), int(end_str[2:])
                s_total = s_hr * 60 + s_mi
                e_total = e_hr * 60 + e_mi
                if abs(e_total - s_total) != 15:
                    continue  # not a 15-min interval
            except ValueError:
                continue

            # Clean OCR artifacts in the values portion
            values_str = _clean_ts_values(m_row.group(3))
            vals = [int(x) for x in re.findall(r'\d+', values_str)]

            if len(vals) != 12:
                continue  # 15-min row must have exactly 12 movement values

            row_dict = dict(zip(_TS_COL_ORDER, vals))
            quarter_data.append({
                "hour": s_hr,
                "minute": s_mi,
                "NL": row_dict.get("NL", 0),
                "NT": row_dict.get("NT", 0),
                "NR": row_dict.get("NR", 0),
                "SL": row_dict.get("SL", 0),
                "ST": row_dict.get("ST", 0),
                "SR": row_dict.get("SR", 0),
                "EL": row_dict.get("EL", 0),
                "ET": row_dict.get("ET", 0),
                "ER": row_dict.get("ER", 0),
                "WL": row_dict.get("WL", 0),
                "WT": row_dict.get("WT", 0),
                "WR": row_dict.get("WR", 0),
            })

    # Aggregate to hourly
    hourly = {}
    for q in quarter_data:
        h = q["hour"]
        if h not in hourly:
            hourly[h] = {k: 0 for k in ["NL", "NT", "NR", "SL", "ST", "SR",
                                          "EL", "ET", "ER", "WL", "WT", "WR"]}
        for k in hourly[h]:
            hourly[h][k] += q[k]

    counts = []
    for h, d in sorted(hourly.items()):
        for approach, lt_k, th_k, rt_k in [
            ("NB", "NL", "NT", "NR"),
            ("SB", "SL", "ST", "SR"),
            ("EB", "EL", "ET", "ER"),
            ("WB", "WL", "WT", "WR"),
        ]:
            total = d[lt_k] + d[th_k] + d[rt_k]
            if total > 0:
                counts.append({
                    "hour_start": h,
                    "approach": approach,
                    "left_turn": d[lt_k],
                    "through": d[th_k],
                    "right_turn": d[rt_k],
                    "total": total,
                })

    return meta, counts, []


# Classic LADOT format regex patterns
RE_MANUAL_NS = re.compile(r"North/South\s*[=:]?\s*(.+)", re.IGNORECASE)
RE_MANUAL_EW = re.compile(r"East/West\s*[=:]?\s*(.+)", re.IGNORECASE)
RE_MANUAL_DATE = re.compile(r"Date:\s*(.+?)(?:\s+Weather|\s*$)", re.IGNORECASE)
RE_MANUAL_DAY = re.compile(r"Day:\s*(\S+)", re.IGNORECASE)
RE_MANUAL_WEATHER = re.compile(r"Weather:\s*(\S+)", re.IGNORECASE)
RE_MANUAL_HOURS = re.compile(r"Hours:\s*(.+)", re.IGNORECASE)
RE_MANUAL_DISTRICT = re.compile(r"District:\s*(\S+)", re.IGNORECASE)
RE_MANUAL_ISCODE = re.compile(r"VS CODE\s+(\S+)", re.IGNORECASE)

# Classic hourly row: "7-8  204 619 0 823  7-8  0 431 203 634  1457  0 0  28 0"
# Approach table: hours Lt Th Rt Total (for NB/SB pair, then EB/WB pair)
RE_CLASSIC_APPROACH_ROW = re.compile(
    r"(\d{1,2})-(\d{1,2})\s+"         # hour range
    r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)"  # Lt Th Rt Total
)

# Pedestrian crossing data in classic format
# e.g. "0 0  28 0" at end of NB/SB rows => S/L ped sch  N/L ped sch
RE_CLASSIC_PED = re.compile(
    r"(\d{1,2})-(\d{1,2})\s+"
    r"\d+\s+\d+\s+\d+\s+\d+\s+"   # first approach (NB or EB)
    r"\d{1,2}-\d{1,2}\s+"
    r"\d+\s+\d+\s+\d+\s+\d+\s+"   # second approach (SB or WB)
    r"\d+\s+"                       # N-S or E-W total
    r"(\d+)\s+(\d+)\s+"            # first crossing: ped, sch
    r"(\d+)\s+(\d+)"               # second crossing: ped, sch
)

# NDS (newer) format: quarter-hour rows
# "9:00 PM  18  40  0  0  36  13  0  0  35  20  6  142"
RE_NDS_ROW = re.compile(
    r"(\d{1,2}):(\d{2})\s*(AM|PM)\s+"
    r"(\d+)\s+(\d+)\s+(\d+)\s+(?:\d+\s+)?"   # NL NT NR [NU]
    r"(\d+)\s+(\d+)\s+(\d+)\s+(?:\d+\s+)?"   # SL ST SR [SU]
    r"(\d+)\s+(\d+)\s+(\d+)\s+(?:\d+\s+)?"   # EL ET ER [EU]
    r"(\d+)\s+(\d+)\s+(\d+)\s+(?:\d+\s+)?"   # WL WT WR [WU]
    r"(\d+)",                                   # TOTAL
    re.IGNORECASE,
)


def parse_classic_manual(text, pages_text):
    """Parse classic LADOT manual count format."""
    meta = {}
    m = RE_MANUAL_NS.search(text)
    meta["ns_street"] = clean_street_name(m.group(1)) if m else None
    m = RE_MANUAL_EW.search(text)
    meta["ew_street"] = clean_street_name(m.group(1)) if m else None
    m = RE_MANUAL_DATE.search(text)
    meta["date"] = normalize_date(m.group(1).strip()) if m else None
    m = RE_MANUAL_DAY.search(text)
    meta["day_of_week"] = normalize_day(m.group(1).strip()) if m else None
    m = RE_MANUAL_WEATHER.search(text)
    meta["weather"] = m.group(1).strip() if m else None
    m = RE_MANUAL_HOURS.search(text)
    meta["count_hours"] = m.group(1).strip() if m else None
    m = RE_MANUAL_DISTRICT.search(text)
    meta["district"] = m.group(1).strip() if m else None
    m = RE_MANUAL_ISCODE.search(text)
    meta["is_code"] = 1 if m else 0

    # Parse approach tables
    # The classic format has two table blocks:
    # Block 1: NORTHBOUND ... SOUTHBOUND ... TOTAL ... XING S/L ... XING N/L
    # Block 2: EASTBOUND  ... WESTBOUND  ... TOTAL ... XING W/L ... XING E/L
    counts = []
    peds = []

    # Find approach sections by looking for "NORTHBOUND Approach" etc.
    nb_section = re.search(r"NORTHBOUND\s+Approach(.*?)TOTAL\s+\d", text, re.DOTALL | re.IGNORECASE)
    sb_section = re.search(r"SOUTHBOUND\s+Approach(.*?)TOTAL\s+\d", text, re.DOTALL | re.IGNORECASE)
    eb_section = re.search(r"EASTBOUND\s+Approach(.*?)TOTAL\s+\d", text, re.DOTALL | re.IGNORECASE)
    wb_section = re.search(r"WESTBOUND\s+Approach(.*?)TOTAL\s+\d", text, re.DOTALL | re.IGNORECASE)

    # Simpler approach: parse line by line looking for "X-Y  Lt Th Rt Total" patterns
    # after approach headers
    lines = text.split("\n")
    current_block = None  # 'NS' or 'EW'
    ns_rows = []
    ew_rows = []

    for line in lines:
        if re.search(r"NORTHBOUND\s+Approach.*SOUTHBOUND\s+Approach", line, re.IGNORECASE):
            current_block = "NS"
            continue
        if re.search(r"EASTBOUND\s+Approach.*WESTBOUND\s+Approach", line, re.IGNORECASE):
            current_block = "EW"
            continue
        if current_block and line.strip().startswith("TOTAL"):
            current_block = None
            continue

        if current_block:
            # Try to parse a dual-approach row
            # Format: "7-8  Lt Th Rt Total  7-8  Lt Th Rt Total  GrandTotal  Ped Sch  Ped Sch"
            # Some PDFs use "7 - 8" with spaces around the dash
            m = re.match(
                r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s+"
                r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+"
                r"(\d{1,2})\s*-\s*(\d{1,2})\s+"
                r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+"
                r"(\d+)\s+"
                r"(\d+)\s+(\d+)\s+"
                r"(\d+)\s+(\d+)",
                line,
            )
            if m:
                hour_start = int(m.group(1))
                if current_block == "NS":
                    approach_a, approach_b = "NB", "SB"
                    crossing_a, crossing_b = "S/L", "N/L"
                else:
                    approach_a, approach_b = "EB", "WB"
                    crossing_a, crossing_b = "W/L", "E/L"

                counts.append({
                    "hour_start": hour_start,
                    "approach": approach_a,
                    "left_turn": int(m.group(3)),
                    "through": int(m.group(4)),
                    "right_turn": int(m.group(5)),
                    "total": int(m.group(6)),
                })
                counts.append({
                    "hour_start": hour_start,
                    "approach": approach_b,
                    "left_turn": int(m.group(9)),
                    "through": int(m.group(10)),
                    "right_turn": int(m.group(11)),
                    "total": int(m.group(12)),
                })
                peds.append({
                    "hour_start": hour_start,
                    "crossing": crossing_a,
                    "pedestrians": int(m.group(14)),
                    "school_children": int(m.group(15)),
                })
                peds.append({
                    "hour_start": hour_start,
                    "crossing": crossing_b,
                    "pedestrians": int(m.group(16)),
                    "school_children": int(m.group(17)),
                })
                continue

            # Try simpler pattern without ped data
            m = re.match(
                r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s+"
                r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+"
                r"(\d{1,2})\s*-\s*(\d{1,2})\s+"
                r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)",
                line,
            )
            if m:
                hour_start = int(m.group(1))
                if current_block == "NS":
                    approach_a, approach_b = "NB", "SB"
                else:
                    approach_a, approach_b = "EB", "WB"

                counts.append({
                    "hour_start": hour_start,
                    "approach": approach_a,
                    "left_turn": int(m.group(3)),
                    "through": int(m.group(4)),
                    "right_turn": int(m.group(5)),
                    "total": int(m.group(6)),
                })
                counts.append({
                    "hour_start": hour_start,
                    "approach": approach_b,
                    "left_turn": int(m.group(9)),
                    "through": int(m.group(10)),
                    "right_turn": int(m.group(11)),
                    "total": int(m.group(12)),
                })

    return meta, counts, peds


def parse_nds_manual(text, pages_text):
    """Parse NDS (National Data & Surveying Services) manual count format."""
    meta = {}
    # NDS format header
    m = re.search(r"Project\s*(?:ID|#):\s*(\S+)", text, re.IGNORECASE)
    meta["staff"] = m.group(1).strip() if m else None
    m = re.search(r"Date:\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, re.IGNORECASE)
    meta["date"] = parse_date_mdy(m.group(1)) if m else None
    m = re.search(r"Day:\s*(\S+)", text, re.IGNORECASE)
    meta["day_of_week"] = normalize_day(m.group(1).strip()) if m else None

    # Try to get street names from first page
    m = re.search(r"(\w[\w\s]+?)\s+and\s+(\w[\w\s]+?)\s*[,.]", text, re.IGNORECASE)
    if m:
        meta["ns_street"] = m.group(1).strip()
        meta["ew_street"] = m.group(2).strip()
    else:
        meta["ns_street"] = None
        meta["ew_street"] = None
    meta["weather"] = None
    meta["district"] = None
    meta["is_code"] = 0
    meta["count_hours"] = None

    # Parse quarter-hour rows from vehicle pages only, aggregate to hourly.
    # Multi-page NDS PDFs have separate pages for Bikes and Pedestrians
    # with the same row format — skip those to avoid contaminating vehicle data.
    quarter_data = []
    for page_text in pages_text:
        if re.search(r"\bBikes?\b|\bPedestrians?\b|\bPeds?\b", page_text[:200], re.IGNORECASE):
            continue
        for m in RE_NDS_ROW.finditer(page_text):
            hr = int(m.group(1))
            minute = int(m.group(2))
            ampm = m.group(3).upper()
            if ampm == "PM" and hr != 12:
                hr += 12
            elif ampm == "AM" and hr == 12:
                hr = 0
            quarter_data.append({
                "hour": hr,
                "NL": int(m.group(4)), "NT": int(m.group(5)), "NR": int(m.group(6)),
                "SL": int(m.group(7)), "ST": int(m.group(8)), "SR": int(m.group(9)),
                "EL": int(m.group(10)), "ET": int(m.group(11)), "ER": int(m.group(12)),
                "WL": int(m.group(13)), "WT": int(m.group(14)), "WR": int(m.group(15)),
            })

    # Aggregate quarters to hourly by approach
    hourly = {}
    for q in quarter_data:
        h = q["hour"]
        if h not in hourly:
            hourly[h] = {"NL": 0, "NT": 0, "NR": 0, "SL": 0, "ST": 0, "SR": 0,
                         "EL": 0, "ET": 0, "ER": 0, "WL": 0, "WT": 0, "WR": 0}
        for k in hourly[h]:
            hourly[h][k] += q[k]

    counts = []
    for h, d in sorted(hourly.items()):
        for approach, lt_key, th_key, rt_key in [
            ("NB", "NL", "NT", "NR"),
            ("SB", "SL", "ST", "SR"),
            ("EB", "EL", "ET", "ER"),
            ("WB", "WL", "WT", "WR"),
        ]:
            total = d[lt_key] + d[th_key] + d[rt_key]
            if total > 0:
                counts.append({
                    "hour_start": h,
                    "approach": approach,
                    "left_turn": d[lt_key],
                    "through": d[th_key],
                    "right_turn": d[rt_key],
                    "total": total,
                })

    return meta, counts, []


def parse_manual_pdf(filepath):
    """Parse a manual count PDF. Tries pdfplumber text first, falls back to OCR."""
    # Try pdfplumber text extraction first (much more reliable than OCR)
    with pdfplumber.open(filepath) as pdf:
        pages_text = [page.extract_text() or "" for page in pdf.pages]
    text = "\n".join(pages_text)

    # Fall back to OCR only if pdfplumber yields very little text
    if len(text.strip()) < 100:
        pages_text = ocr_pdf(filepath)
        text = "\n".join(pages_text)

    if not text.strip():
        raise ValueError("Empty text extraction")

    # --- Format detection ---

    is_classic = bool(re.search(
        r"TRAFFIC COUNT SUMMARY|Department of Transportation", text, re.IGNORECASE
    ))
    is_nds = bool(re.search(
        r"National Data|Intersection Turning Movement", text, re.IGNORECASE
    ))
    is_southland = bool(re.search(r"Southland Car Counters", text, re.IGNORECASE))
    is_traffic_solution = bool(re.search(r"The Traffic Solution|Traffic Solution", text, re.IGNORECASE))

    # "Traffic Solution" format (INTERSECTION TURNING MOVEMENT COUNT SUMMARY
    # with SBRT/SBTH/SBLT columns) — check before classic because the LADOT
    # summary page inside the same PDF is often garbled by OCR.
    if is_traffic_solution and re.search(r'SBRT\s*[\|]?\s*SBTH', text, re.IGNORECASE):
        meta, counts, peds = parse_traffic_solution_manual(text, pages_text)
        if counts:
            return meta, counts, peds

    # Southland Car Counters format (NL NT NR … VOLUMES = row).
    if is_southland and re.search(r'VOLUMES\s*=', text, re.IGNORECASE):
        meta, counts, peds = parse_southland_manual(text, pages_text)
        if counts:
            return meta, counts, peds

    # Classic LADOT format. Some classic PDFs also contain NDS quarter-hour
    # rows (hybrid format) — try classic first for better metadata.
    if is_classic:
        meta, counts, peds = parse_classic_manual(text, pages_text)
        if counts:
            return meta, counts, peds
        # Classic header detected but standard regex yielded nothing —
        # the PDF was likely OCR'd and has table-border artifacts.
        meta, counts, peds = parse_classic_manual_ocr(text)
        if counts:
            return meta, counts, peds

    if is_nds or RE_NDS_ROW.search(text):
        meta, counts, peds = parse_nds_manual(text, pages_text)
        if counts:
            return meta, counts, peds

    # Unknown format: try all parsers in order
    for parser in [
        lambda: parse_classic_manual(text, pages_text),
        lambda: parse_classic_manual_ocr(text),
        lambda: parse_nds_manual(text, pages_text),
        lambda: parse_southland_manual(text, pages_text),
    ]:
        meta, counts, peds = parser()
        if counts:
            return meta, counts, peds

    # Return whatever the last attempt produced (may have empty counts)
    return parse_nds_manual(text, pages_text)


def step_parse_manual(conn):
    rows = conn.execute(
        "SELECT id, traffic_id, filename FROM pdfs WHERE type='manual' AND downloaded=1 AND parsed=0"
    ).fetchall()
    print(f"Parsing {len(rows)} manual count PDFs...")

    success = 0
    errors = 0
    for pdf_id, traffic_id, filename in tqdm(rows, desc="Parsing manual"):
        filepath = PDF_MANUAL_DIR / filename
        if not filepath.exists():
            conn.execute("UPDATE pdfs SET parse_error = 'file missing' WHERE id = ?", (pdf_id,))
            errors += 1
            continue
        try:
            meta, counts, peds = parse_manual_pdf(filepath)
        except Exception as e:
            conn.execute("UPDATE pdfs SET parse_error = ? WHERE id = ?", (str(e)[:500], pdf_id))
            errors += 1
            continue

        if not counts:
            conn.execute("UPDATE pdfs SET parse_error = 'no count rows parsed' WHERE id = ?", (pdf_id,))
            errors += 1
            continue

        for c in counts:
            conn.execute(
                """INSERT INTO manual_counts
                (pdf_id, traffic_id, ns_street, ew_street, date, day_of_week,
                 weather, staff, district, is_code, count_hours,
                 hour_start, approach, left_turn, through, right_turn, total)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pdf_id, traffic_id, meta.get("ns_street"), meta.get("ew_street"),
                    meta.get("date"), meta.get("day_of_week"), meta.get("weather"),
                    meta.get("staff"), meta.get("district"), meta.get("is_code"),
                    meta.get("count_hours"),
                    c["hour_start"], c["approach"],
                    c["left_turn"], c["through"], c["right_turn"], c["total"],
                ),
            )

        for p in peds:
            conn.execute(
                """INSERT INTO manual_ped_counts
                (pdf_id, traffic_id, hour_start, crossing, pedestrians, school_children)
                VALUES (?,?,?,?,?,?)""",
                (pdf_id, traffic_id, p["hour_start"], p["crossing"],
                 p["pedestrians"], p["school_children"]),
            )

        conn.execute("UPDATE pdfs SET parsed = 1 WHERE id = ?", (pdf_id,))
        success += 1

        if (success + errors) % 50 == 0:
            conn.commit()

    conn.commit()
    print(f"  Parsed: {success}, Errors: {errors}")


# ---------------------------------------------------------------------------
# Step 6: Fix/normalize existing data
# ---------------------------------------------------------------------------

def step_fix_data(conn):
    """Post-parse data normalization."""
    print("Normalizing existing data...")
    fixes = 0

    # Fix 'None' strings → SQL NULL in manual_counts
    for col in ["ns_street", "ew_street", "date", "day_of_week", "weather",
                 "staff", "district", "count_hours"]:
        n = conn.execute(
            f"UPDATE manual_counts SET {col} = NULL WHERE {col} = 'None'"
        ).rowcount
        if n:
            print(f"  Fixed {n} 'None' strings in manual_counts.{col}")
            fixes += n

    # Fix 'None' strings in automatic_counts
    for col in ["location", "date", "day_of_week", "weather", "counter",
                 "sensor_layout", "dot_district"]:
        n = conn.execute(
            f"UPDATE automatic_counts SET {col} = NULL WHERE {col} = 'None'"
        ).rowcount
        if n:
            print(f"  Fixed {n} 'None' strings in automatic_counts.{col}")
            fixes += n

    # Normalize dates in manual_counts
    rows = conn.execute(
        "SELECT DISTINCT date FROM manual_counts WHERE date IS NOT NULL "
        "AND date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
    ).fetchall()
    for (raw_date,) in rows:
        normalized = normalize_date(raw_date)
        if normalized and normalized != raw_date:
            n = conn.execute(
                "UPDATE manual_counts SET date = ? WHERE date = ?",
                (normalized, raw_date),
            ).rowcount
            fixes += n

    # Normalize day_of_week in all tables
    for table in ["automatic_counts", "manual_counts"]:
        rows = conn.execute(
            f"SELECT DISTINCT day_of_week FROM {table} WHERE day_of_week IS NOT NULL"
        ).fetchall()
        for (raw_day,) in rows:
            normalized = normalize_day(raw_day)
            if normalized != raw_day:
                n = conn.execute(
                    f"UPDATE {table} SET day_of_week = ? WHERE day_of_week = ?",
                    (normalized, raw_day),
                ).rowcount
                if n:
                    print(f"  Normalized '{raw_day}' → '{normalized}' in {table} ({n} rows)")
                    fixes += n

    # Set quarter columns to NULL when all are 0 but total > 0
    n = conn.execute("""
        UPDATE automatic_counts SET
            nw_q1 = NULL, nw_q2 = NULL, nw_q3 = NULL, nw_q4 = NULL,
            se_q1 = NULL, se_q2 = NULL, se_q3 = NULL, se_q4 = NULL
        WHERE nw_q1 = 0 AND nw_q2 = 0 AND nw_q3 = 0 AND nw_q4 = 0
          AND se_q1 = 0 AND se_q2 = 0 AND se_q3 = 0 AND se_q4 = 0
          AND (nw_total > 0 OR se_total > 0)
    """).rowcount
    if n:
        print(f"  Set quarter columns to NULL for {n} rows with no breakdown")
        fixes += n

    conn.commit()
    print(f"  Total fixes: {fixes}")


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def print_stats(conn):
    print("\n=== Database Stats ===")
    for table in ["intersections", "segments", "pdfs", "automatic_counts", "manual_counts", "manual_ped_counts"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count:,}")

    auto_pdfs = conn.execute("SELECT COUNT(*) FROM pdfs WHERE type='automatic'").fetchone()[0]
    manual_pdfs = conn.execute("SELECT COUNT(*) FROM pdfs WHERE type='manual'").fetchone()[0]
    downloaded = conn.execute("SELECT COUNT(*) FROM pdfs WHERE downloaded=1").fetchone()[0]
    parsed = conn.execute("SELECT COUNT(*) FROM pdfs WHERE parsed=1").fetchone()[0]
    errored = conn.execute("SELECT COUNT(*) FROM pdfs WHERE parse_error IS NOT NULL").fetchone()[0]
    print(f"\n  PDFs: {auto_pdfs:,} automatic, {manual_pdfs:,} manual")
    print(f"  Downloaded: {downloaded:,}, Parsed: {parsed:,}, Errors: {errored:,}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="NavigateLA Traffic Data Pipeline")
    parser.add_argument("--step", required=True,
                        choices=["index", "scrape", "download", "parse-auto", "parse-manual", "fix-data", "stats", "all"],
                        help="Pipeline step to run")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of items to process (for testing)")
    args = parser.parse_args()

    # Set up logging: warnings to stderr, download errors to file
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    log_file = DATA_DIR / "download_errors.log"
    fh = logging.FileHandler(log_file, mode="a")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(fh)
    log.setLevel(logging.DEBUG)

    conn = init_db()

    try:
        if args.step in ("index", "all"):
            step_index(conn)

        if args.step in ("scrape", "all"):
            asyncio.run(step_scrape(conn, limit=args.limit))

        if args.step in ("download", "all"):
            asyncio.run(step_download(conn, limit=args.limit))

        if args.step in ("parse-auto", "all"):
            step_parse_auto(conn)

        if args.step in ("parse-manual", "all"):
            step_parse_manual(conn)

        if args.step in ("fix-data", "all"):
            step_fix_data(conn)

        print_stats(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
