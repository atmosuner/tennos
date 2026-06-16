from __future__ import annotations

import argparse
import html
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = "https://www.ikort.com.tr"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETAILS_DIR = PROJECT_ROOT / "outputs" / "tournament_details"
CACHE_DIR = PROJECT_ROOT / "work" / "ikort_clubs_cache"
CLUBS_OUTPUT = PROJECT_ROOT / "outputs" / "clubs.json"
MAP_OUTPUT = PROJECT_ROOT / "outputs" / "club_abbrev_map.json"

LIST_COLUMNS = ["address", "email", "city", "phone", "web", "contact"]


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split()).strip()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8", errors="ignore")
    tmp.replace(path)


class Fetcher:
    def __init__(self, cache_dir: Path, delay: float, jitter: float, timeout: float, retries: int, refresh: bool) -> None:
        self.cache_dir = cache_dir
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.retries = retries
        self.refresh = refresh
        self.last_at = 0.0

    def _sleep(self) -> None:
        wait = self.delay + random.uniform(0, self.jitter)
        elapsed = time.monotonic() - self.last_at
        if self.last_at and elapsed < wait:
            time.sleep(wait - elapsed)

    def get(self, url: str, cache_path: Path) -> str:
        if not self.refresh and cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="ignore")
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.6",
        }
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._sleep()
            request = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    text = raw.decode(charset, errors="ignore")
                    self.last_at = time.monotonic()
                    atomic_write_text(cache_path, text)
                    return text
            except (urllib.error.HTTPError, urllib.error.URLError) as exc:
                self.last_at = time.monotonic()
                last_error = exc
                if attempt < self.retries:
                    time.sleep((attempt + 1) * max(self.delay, 1))
                else:
                    raise
        raise RuntimeError(f"Fetch failed for {url}: {last_error}")


def parse_club_rows(list_html: str) -> list[dict[str, Any]]:
    clubs = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", list_html, flags=re.S | re.I):
        id_match = re.search(r"kulup-detay/(\d+)", row)
        if not id_match:
            continue
        club_id = int(id_match.group(1))
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)
        # Anchor on the cell holding the kulup-detay link; columns after it are
        # address, email, city, phone, web, contact, (actions).
        link_index = next((i for i, cell in enumerate(cells) if "kulup-detay" in cell), None)
        if link_index is None:
            continue
        name_match = re.search(r"<a[^>]*kulup-detay/\d+[^>]*>(.*?)</a>", cells[link_index], flags=re.S | re.I)
        name = clean_text(name_match.group(1)) if name_match else clean_text(cells[link_index])
        tail = [clean_text(cell) for cell in cells[link_index + 1 : link_index + 1 + len(LIST_COLUMNS)]]
        record: dict[str, Any] = {"clubId": club_id, "name": name}
        for key, value in zip(LIST_COLUMNS, tail):
            record[key] = value
        clubs.append(record)
    return clubs


def scrape_club_list(fetcher: Fetcher, max_pages: int) -> list[dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for page in range(1, max_pages + 1):
        url = f"{BASE_URL}/kulupler?{urllib.parse.urlencode({'page': page})}"
        text = fetcher.get(url, CACHE_DIR / f"list_page_{page}.html")
        rows = parse_club_rows(text)
        new = [club for club in rows if club["clubId"] not in by_id]
        for club in rows:
            by_id.setdefault(club["clubId"], club)
        print(f"  page={page} rows={len(rows)} new={len(new)} total={len(by_id)}", flush=True)
        if not new:
            break
    return sorted(by_id.values(), key=lambda club: club["clubId"])


def distinct_abbreviations(details_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    pattern = re.compile(r"\(([^)]+)\)\s*$")
    for path in sorted(details_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for day in payload.get("matchSchedule", []):
            for match in day.get("matches", []):
                for player in match.get("players", []):
                    name = player.get("name") or ""
                    found = pattern.search(name)
                    if found:
                        abbrev = found.group(1).strip()
                        if abbrev:
                            counts[abbrev] = counts.get(abbrev, 0) + 1
    return counts


def search_clubs_by_abbrev(fetcher: Fetcher, abbrev: str) -> list[dict[str, Any]]:
    url = f"{BASE_URL}/kulupler?{urllib.parse.urlencode({'short_name': abbrev})}"
    safe = re.sub(r"[^A-Za-z0-9]+", "_", abbrev).strip("_") or "x"
    text = fetcher.get(url, CACHE_DIR / f"search_{safe}.html")
    results = []
    for club in parse_club_rows(text):
        results.append({"clubId": club["clubId"], "name": club["name"], "city": club.get("city", "")})
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape I-KORT club master list and abbreviation -> club bridge.")
    parser.add_argument("--details-dir", type=Path, default=DETAILS_DIR, help="Tournament detail JSON dir (source of abbreviations).")
    parser.add_argument("--max-pages", type=int, default=20, help="Max club list pages to crawl.")
    parser.add_argument("--delay", type=float, default=1.5, help="Minimum delay between requests.")
    parser.add_argument("--jitter", type=float, default=1.0, help="Random extra delay.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Retry count.")
    parser.add_argument("--refresh", action="store_true", help="Refetch even if cached.")
    parser.add_argument("--skip-list", action="store_true", help="Skip club master list crawl.")
    parser.add_argument("--skip-map", action="store_true", help="Skip abbreviation bridge.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    fetcher = Fetcher(CACHE_DIR, args.delay, args.jitter, args.timeout, args.retries, args.refresh)

    if not args.skip_list:
        print("scraping club master list...")
        clubs = scrape_club_list(fetcher, args.max_pages)
        atomic_write_json(CLUBS_OUTPUT, {"clubs": clubs})
        print(f"clubs written: {len(clubs)} -> {CLUBS_OUTPUT}")

    if not args.skip_map:
        print("building abbreviation -> club bridge...")
        counts = distinct_abbreviations(args.details_dir)
        ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        print(f"distinct abbreviations: {len(ordered)}")
        mapping: list[dict[str, Any]] = []
        stats = {"unique": 0, "none": 0, "ambiguous": 0}
        for index, (abbrev, occurrences) in enumerate(ordered, start=1):
            results = search_clubs_by_abbrev(fetcher, abbrev)
            if len(results) == 1:
                status = "unique"
            elif not results:
                status = "none"
            else:
                status = "ambiguous"
            stats[status] += 1
            mapping.append(
                {
                    "abbrev": abbrev,
                    "occurrences": occurrences,
                    "status": status,
                    "matches": results,
                    "clubId": results[0]["clubId"] if status == "unique" else None,
                }
            )
            print(f"  [{index}/{len(ordered)}] {abbrev} ({occurrences}) -> {status} {len(results)}", flush=True)
        atomic_write_json(
            MAP_OUTPUT,
            {"stats": stats, "total": len(ordered), "map": mapping},
        )
        print(f"map written: {stats} -> {MAP_OUTPUT}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
