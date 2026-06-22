"""Scrape ikort.com.tr klasman puanı tables and store in tennos.db.

Usage:
  python work/scrape_klasman_puan.py                         # all weeks of current year
  python work/scrape_klasman_puan.py --year 2025             # all weeks of 2025
  python work/scrape_klasman_puan.py --week 25               # specific week, current year
  python work/scrape_klasman_puan.py --year 2026 --from-week 20 --to-week 25
  python work/scrape_klasman_puan.py --force                 # overwrite existing rows
  python work/scrape_klasman_puan.py --no-ssl-verify         # for corporate proxies
"""

from __future__ import annotations

import argparse
import datetime
import random
import re
import sqlite3
import ssl
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = PROJECT_ROOT / "outputs"
DB_PATH = OUTPUTS / "tennos.db"
BASE_URL = "https://ikort.com.tr/filter-klasman-puani"
TYPES = ["KLASMAN_12", "KLASMAN_14"]
GENDERS = [1, 2]


# ── helpers ───────────────────────────────────────────────────────────────────

def current_iso_week() -> int:
    return datetime.date.today().isocalendar()[1]


def current_year() -> int:
    return datetime.date.today().year


def normalize_name(name: str) -> str:
    """Uppercase + fold Turkish diacritics for fuzzy matching."""
    name = (name or "").upper().strip()
    tr = str.maketrans({
        "ğ": "g", "Ğ": "G", "ı": "i", "İ": "I",
        "ş": "s", "Ş": "S", "ö": "o", "Ö": "O",
        "ü": "u", "Ü": "U", "ç": "c", "Ç": "C",
    })
    name = name.translate(tr)
    name = unicodedata.normalize("NFD", name)
    return "".join(c for c in name if unicodedata.category(c) != "Mn")


def int_or_none(val: str) -> int | None:
    try:
        return int(val.replace(".", "").replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


# ── HTML parser ───────────────────────────────────────────────────────────────

class KlasmanTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_tbody = False
        self.in_tr = False
        self.current_cell: dict[str, Any] | None = None
        self.current_row: list[dict[str, Any]] = []
        self.rows: list[list[dict[str, Any]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "tbody":
            self.in_tbody = True
        elif tag == "tr" and self.in_tbody:
            self.in_tr = True
            self.current_row = []
        elif tag in ("th", "td") and self.in_tr:
            self.current_cell = {"text": "", "href": None}
        elif tag == "a" and self.current_cell is not None:
            self.current_cell["href"] = a.get("href")

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "tbody":
            self.in_tbody = False
        elif tag == "tr" and self.in_tr:
            self.in_tr = False
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = []
        elif tag in ("th", "td") and self.current_cell is not None:
            self.current_cell["text"] = " ".join(self.current_cell["text"].split())
            self.current_row.append(self.current_cell)
            self.current_cell = None


# ── fetch ─────────────────────────────────────────────────────────────────────

def fetch_html(url: str, timeout: float, ssl_ctx: ssl.SSLContext | None) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; tennos-scraper/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── parse ─────────────────────────────────────────────────────────────────────

def extract_detay_id(href: str | None) -> int | None:
    if not href:
        return None
    m = re.search(r"/(\d+)$", href)
    return int(m.group(1)) if m else None


def parse_rows(html_content: str) -> list[dict[str, Any]]:
    parser = KlasmanTableParser()
    parser.feed(html_content)
    records: list[dict[str, Any]] = []
    for row in parser.rows:
        if len(row) < 9:
            continue
        texts = [c["text"] for c in row]
        hrefs = [c.get("href") for c in row]
        if not texts[0].isdigit():  # skip header rows
            continue
        records.append({
            "klasman_sira": int_or_none(texts[0]),
            "ulusal_sira":  int_or_none(texts[1]),
            "raw_name":     texts[2],
            "puan":         int_or_none(texts[3]),
            "ulusal_puan":  int_or_none(texts[4]),
            "uluslar_puan": int_or_none(texts[5]),
            "kulup_adi":    texts[6],
            "birth_year":   int_or_none(texts[7]),
            "detay_id":     extract_detay_id(hrefs[8]) if len(hrefs) > 8 else None,
        })
    return records


# ── DB ────────────────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS klasman_puan (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    year          INTEGER NOT NULL,
    week          INTEGER NOT NULL,
    type          TEXT    NOT NULL,
    gender        INTEGER NOT NULL,
    klasman_sira  INTEGER,
    ulusal_sira   INTEGER,
    raw_name      TEXT,
    puan          INTEGER,
    ulusal_puan   INTEGER,
    uluslar_puan  INTEGER,
    kulup_adi     TEXT,
    birth_year    INTEGER,
    detay_id      INTEGER,
    player_id     INTEGER,
    UNIQUE(year, week, type, gender, detay_id)
);
CREATE INDEX IF NOT EXISTS idx_kp_player ON klasman_puan(player_id);
CREATE INDEX IF NOT EXISTS idx_kp_week   ON klasman_puan(year, week, type, gender);
"""


def build_player_lookup(cur: sqlite3.Cursor) -> dict[tuple[str, int], int]:
    cur.execute("SELECT player_id, name, birth_year FROM players WHERE birth_year IS NOT NULL")
    lookup: dict[tuple[str, int], int] = {}
    for pid, name, year in cur.fetchall():
        key = (normalize_name(name or ""), year)
        lookup.setdefault(key, pid)  # first wins on rare duplicates
    return lookup


def combo_exists(cur: sqlite3.Cursor, year: int, week: int, typ: str, gender: int) -> bool:
    cur.execute(
        "SELECT 1 FROM klasman_puan WHERE year=? AND week=? AND type=? AND gender=? LIMIT 1",
        (year, week, typ, gender),
    )
    return cur.fetchone() is not None


def upsert_rows(
    cur: sqlite3.Cursor,
    year: int, week: int, typ: str, gender: int,
    rows: list[dict[str, Any]],
    lookup: dict[tuple[str, int], int],
    force: bool,
) -> tuple[int, int]:
    if force:
        cur.execute(
            "DELETE FROM klasman_puan WHERE year=? AND week=? AND type=? AND gender=?",
            (year, week, typ, gender),
        )
    inserted = matched = 0
    for r in rows:
        key = (normalize_name(r["raw_name"]), r["birth_year"])
        player_id = lookup.get(key)
        if player_id:
            matched += 1
        cur.execute(
            """
            INSERT OR IGNORE INTO klasman_puan
                (year, week, type, gender, klasman_sira, ulusal_sira, raw_name,
                 puan, ulusal_puan, uluslar_puan, kulup_adi, birth_year, detay_id, player_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (year, week, typ, gender,
             r["klasman_sira"], r["ulusal_sira"], r["raw_name"],
             r["puan"], r["ulusal_puan"], r["uluslar_puan"],
             r["kulup_adi"], r["birth_year"], r["detay_id"], player_id),
        )
        inserted += cur.rowcount
    return inserted, matched


# ── worker ───────────────────────────────────────────────────────────────────

def fetch_combo(
    year: int, week: int, typ: str, gender: int,
    ssl_ctx: ssl.SSLContext | None,
    timeout: float,
    stagger: float,
) -> tuple[int, int, str, int, list[dict[str, Any]], str | None]:
    """Fetch + parse one combo. Returns (year, week, type, gender, rows, error)."""
    if stagger > 0:
        time.sleep(random.uniform(0, stagger))
    url = f"{BASE_URL}?gender={gender}&date={year}&type={typ}&week={week}"
    try:
        content = fetch_html(url, timeout=timeout, ssl_ctx=ssl_ctx)
        rows = parse_rows(content)
        return (year, week, typ, gender, rows, None)
    except Exception as e:
        return (year, week, typ, gender, [], str(e))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape ikort klasman puanı into tennos.db")
    ap.add_argument("--year", type=int, default=current_year(),
                    help="Year to scrape (default: current year)")
    ap.add_argument("--week", type=int, default=None,
                    help="Single week number")
    ap.add_argument("--from-week", type=int, default=1,
                    help="Start of week range (default: 1)")
    ap.add_argument("--to-week", type=int, default=None,
                    help="End of week range (default: auto)")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing rows")
    ap.add_argument("--workers", type=int, default=5,
                    help="Parallel HTTP workers (default: 5)")
    ap.add_argument("--no-ssl-verify", action="store_true",
                    help="Disable SSL certificate verification")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print plan without fetching or writing")
    ap.add_argument("--stagger", type=float, default=0.5,
                    help="Max random delay (s) before each worker request (default: 0.5)")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="HTTP timeout seconds (default: 20)")
    args = ap.parse_args()

    ssl_ctx: ssl.SSLContext | None = None
    if args.no_ssl_verify:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    # Determine week list
    if args.week is not None:
        weeks = [args.week]
    else:
        if args.to_week is not None:
            max_week = args.to_week
        elif args.year == current_year():
            max_week = current_iso_week() - 1
        else:
            max_week = 52
        weeks = list(range(args.from_week, max_week + 1))

    combos = [(t, g) for t in TYPES for g in GENDERS]
    total_requests = len(weeks) * len(combos)
    week_range = f"{weeks[0]}–{weeks[-1]}" if len(weeks) > 1 else str(weeks[0])
    print(
        f"year={args.year}  weeks={week_range} ({len(weeks)})  "
        f"combos={len(combos)}  total_requests={total_requests}  "
        f"workers={args.workers}  force={args.force}"
    )

    if args.dry_run:
        print("--dry-run: no fetches.")
        return

    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(DB_PATH, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")  # retry up to 60s on lock
    cur = con.cursor()
    cur.executescript(CREATE_TABLE_SQL)
    con.commit()

    lookup = build_player_lookup(cur)
    print(f"Player lookup: {len(lookup)} entries\n")

    # Pre-filter: skip combos that already exist
    tasks: list[tuple[int, int, str, int]] = []
    total_skipped = 0
    for week in weeks:
        for typ, gender in combos:
            if not args.force and combo_exists(cur, args.year, week, typ, gender):
                print(f"  skip   {args.year}w{week:02d} {typ} g{gender} (exists)")
                total_skipped += 1
            else:
                tasks.append((args.year, week, typ, gender))

    if not tasks:
        con.close()
        print(f"\ndone  inserted=0  skipped={total_skipped}  empty=0  errors=0")
        return

    print(f"\nFetching {len(tasks)} combos with {args.workers} workers…\n")

    total_inserted = total_empty = total_errors = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_map = {
            pool.submit(fetch_combo, year, week, typ, gender,
                        ssl_ctx, args.timeout, args.stagger): (year, week, typ, gender)
            for year, week, typ, gender in tasks
        }
        for future in as_completed(future_map):
            year, week, typ, gender, rows, err = future.result()
            label = f"{year}w{week:02d} {typ} g{gender}"
            if err:
                print(f"  ERR    {label}  {err}", file=sys.stderr)
                total_errors += 1
                continue
            if not rows:
                print(f"  empty  {label}")
                total_empty += 1
                continue
            inserted, matched = upsert_rows(
                cur, year, week, typ, gender, rows, lookup, args.force
            )
            con.commit()
            print(f"  ok     {label}  rows={len(rows)}  inserted={inserted}  "
                  f"matched={matched}/{len(rows)}")
            total_inserted += inserted

    con.close()
    print(
        f"\ndone  inserted={total_inserted}  skipped={total_skipped}  "
        f"empty={total_empty}  errors={total_errors}"
    )


if __name__ == "__main__":
    main()
