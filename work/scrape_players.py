"""Build outputs/players.json from the public player directory search.

The /oyuncular search accepts a birth-date range and paginates the full result set
(12 rows per page, no result cap). Each row already carries name, birth year, gender,
club (full name) and city, so no per-player profile fetch is needed.

Idempotent and resumable: every page is cached under work/ikort_players_cache, so a
re-run after the range grows only fetches new/uncached pages. Club names are matched
against outputs/clubs.json to attach a club_id when an exact name match exists.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any

from scrape_clubs import BASE_URL, Fetcher, atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = PROJECT_ROOT / "work" / "ikort_players_cache"
CLUBS_FILE = PROJECT_ROOT / "outputs" / "clubs.json"
OUTPUT = PROJECT_ROOT / "outputs" / "players.json"

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
PROFILE_RE = re.compile(r"oyuncu-profil/(\d+)")
NAME_TH_RE = re.compile(r'<th[^>]*d-none[^>]*>(.*?)</th>', re.S | re.I)


def clean_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def load_club_name_index() -> dict[str, int]:
    if not CLUBS_FILE.exists():
        return {}
    payload = json.loads(CLUBS_FILE.read_text(encoding="utf-8"))
    return {club["name"].strip().upper(): club["clubId"] for club in payload["clubs"] if club.get("name")}


def parse_rows(page_html: str, club_index: dict[str, int]) -> list[dict[str, Any]]:
    players = []
    for row in ROW_RE.findall(page_html):
        profile = PROFILE_RE.search(row)
        if not profile:
            continue
        player_id = profile.group(1)
        name_match = NAME_TH_RE.search(row)
        name = clean_text(name_match.group(1)) if name_match else ""
        tds = [clean_text(cell) for cell in TD_RE.findall(row)]
        # tds: [birthYear, gender, club, city, (detay)]
        birth_year = tds[0] if len(tds) > 0 else ""
        gender = tds[1] if len(tds) > 1 else ""
        club_name = tds[2] if len(tds) > 2 else ""
        city = tds[3] if len(tds) > 3 else ""
        if not name:
            name = clean_text(tds[-1]) if tds else ""
        players.append(
            {
                "playerId": int(player_id),
                "name": name,
                "birthYear": int(birth_year) if birth_year.isdigit() else None,
                "gender": gender or None,
                "clubName": club_name or None,
                "clubId": club_index.get(club_name.strip().upper()) if club_name and club_name != "FERDI" else None,
                "city": city or None,
                "profileUrl": f"{BASE_URL}/oyuncu-profil/{player_id}",
            }
        )
    return players


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape the player directory into outputs/players.json.")
    parser.add_argument("--birth-start", default="01-01-2008", help="dd-mm-yyyy")
    parser.add_argument("--birth-end", default="31-12-2018", help="dd-mm-yyyy")
    parser.add_argument("--delay", type=float, default=0.6)
    parser.add_argument("--jitter", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--refresh", action="store_true", help="Refetch pages even if cached.")
    parser.add_argument("--max-pages", type=int, default=5000, help="Safety cap on page count.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    club_index = load_club_name_index()
    fetcher = Fetcher(CACHE_DIR, args.delay, args.jitter, args.timeout, args.retries, args.refresh)
    base = (
        f"{BASE_URL}/oyuncular?name=&surname=&gender="
        f"&birth_start={args.birth_start}&birth_end={args.birth_end}"
    )

    players: dict[int, dict[str, Any]] = {}
    matched_clubs = 0
    page = 1
    while page <= args.max_pages:
        text = fetcher.get(f"{base}&page={page}", CACHE_DIR / f"page_{page}.html")
        rows = parse_rows(text, club_index)
        if not rows:
            break
        for player in rows:
            if player["playerId"] not in players:
                players[player["playerId"]] = player
                if player["clubId"] is not None:
                    matched_clubs += 1
        if page % 50 == 0:
            print(f"  page={page} players={len(players)}", flush=True)
        page += 1

    ordered = sorted(players.values(), key=lambda p: p["playerId"])
    atomic_write_json(
        OUTPUT,
        {
            "birthRange": [args.birth_start, args.birth_end],
            "total": len(ordered),
            "clubIdMatched": matched_clubs,
            "players": ordered,
        },
    )
    print(f"done players={len(ordered)} pages={page - 1} clubIdMatched={matched_clubs} -> {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
