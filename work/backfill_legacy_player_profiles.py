"""One-time backfill: fill birth_year/gender for players missing from the player
directory scrape (scrape_players.py only covers birth years 2008-2018; players whose
last match predates that window, or who were born earlier, never got picked up there).

Fetches each missing player's own profile page (same data the live site shows
anonymously: birth year + gender) and writes it straight into outputs/tennos.db.
Run once; re-running is safe/idempotent (only touches players with gender IS NULL).

Usage:
    python3 work/backfill_legacy_player_profiles.py                # all missing, 8 workers
    python3 work/backfill_legacy_player_profiles.py --dry-run
    python3 work/backfill_legacy_player_profiles.py --workers 12 --limit 200
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "outputs" / "tennos.db"
BASE_URL = "https://www.ikort.com.tr"

BIRTH_RE = re.compile(r"Doğum Tarihi</div>\s*<div[^>]*>\s*(\d{4})", re.S)
GENDER_RE = re.compile(r"Cinsiyet</div>\s*<div[^>]*>\s*(Erkek|Kadın)", re.S)


def fetch_profile(player_id: int, timeout: float, retries: int, ssl_context: ssl.SSLContext | None) -> tuple[int | None, str | None]:
    url = f"{BASE_URL}/oyuncu-profil/{player_id}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "tr-TR,tr;q=0.9"}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
                text = resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="ignore")
            by = BIRTH_RE.search(text)
            ge = GENDER_RE.search(text)
            return (int(by.group(1)) if by else None, ge.group(1) if ge else None)
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    print(f"  fail player_id={player_id}: {last_error}")
    return (None, None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill birth_year/gender for legacy players missing from the directory.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-ssl-verify", action="store_true")
    args = parser.parse_args()

    ssl_context: ssl.SSLContext | None = None
    if args.no_ssl_verify:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    ids = [r[0] for r in cur.execute("SELECT player_id FROM players WHERE gender IS NULL OR gender=''").fetchall()]
    if args.limit:
        ids = ids[: args.limit]
    print(f"missing={len(ids)}")
    if args.dry_run:
        return 0

    found = 0
    updates: list[tuple[int | None, str | None, int]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_profile, pid, args.timeout, args.retries, ssl_context): pid for pid in ids}
        for i, fut in enumerate(as_completed(futures), 1):
            pid = futures[fut]
            birth_year, gender = fut.result()
            if birth_year or gender:
                found += 1
                updates.append((birth_year, gender, pid))
            if i % 250 == 0:
                print(f"  {i}/{len(ids)} fetched, {found} resolved", flush=True)

    cur.executemany(
        "UPDATE players SET birth_year=COALESCE(?,birth_year), gender=COALESCE(?,gender) WHERE player_id=?",
        updates,
    )
    conn.commit()
    conn.close()
    print(f"done attempted={len(ids)} resolved={found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
