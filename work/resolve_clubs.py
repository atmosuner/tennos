"""Resolve player club abbreviations to concrete clubs, incrementally and idempotently.

Resolution order per distinct player (identified by playerId) whose match name ends
with a "(ABBREV)" tag:

    1. abbrev == FERDI            -> unaffiliated, left unresolved (null at ETL time)
    2. abbrev in unique map       -> trusted club from club_abbrev_map.json (no fetch)
    3. already in overrides       -> kept as-is (incremental: never re-fetched)
    4. otherwise                  -> fetch the player's own profile page and read the
                                     single kulup-detay link (definitive, per player)

Re-run after the scraper adds tournaments: only players not yet resolved are fetched.
Profile HTML is cached under work/ikort_clubs_cache, so repeated runs are cheap.

Outputs:
    outputs/player_club_overrides.json   playerId -> {clubId, clubName, abbrev, source}
    outputs/clubs.json                   master club list, extended with discovered clubs
"""

from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from typing import Any

from scrape_clubs import BASE_URL, CACHE_DIR, Fetcher, atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETAILS_DIR = PROJECT_ROOT / "outputs" / "tournament_details"
CLUBS_FILE = PROJECT_ROOT / "outputs" / "clubs.json"
MAP_FILE = PROJECT_ROOT / "outputs" / "club_abbrev_map.json"
OVERRIDES_FILE = PROJECT_ROOT / "outputs" / "player_club_overrides.json"

ABBREV_RE = re.compile(r"\(([^)]+)\)\s*$")
KULUP_LINK_RE = re.compile(r"kulup-detay/(\d+)[^>]*>(.*?)</a>", re.S | re.I)
FERDI = "FERDI"


def clean_text(value: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def load_unique_map() -> dict[str, dict[str, Any]]:
    if not MAP_FILE.exists():
        return {}
    payload = json.loads(MAP_FILE.read_text(encoding="utf-8"))
    return {
        entry["abbrev"]: {"clubId": entry["clubId"], "clubName": (entry["matches"][0]["name"] if entry["matches"] else "")}
        for entry in payload.get("map", [])
        if entry["status"] == "unique"
    }


def load_clubs() -> dict[int, dict[str, Any]]:
    if not CLUBS_FILE.exists():
        return {}
    payload = json.loads(CLUBS_FILE.read_text(encoding="utf-8"))
    return {club["clubId"]: club for club in payload["clubs"]}


def load_overrides() -> dict[str, dict[str, Any]]:
    if not OVERRIDES_FILE.exists():
        return {}
    payload = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    return payload.get("overrides", {})


def distinct_tagged_players(details_dir: Path) -> dict[str, tuple[str, str]]:
    """playerId -> (name, abbrev) for every player whose name ends with (abbrev)."""
    players: dict[str, tuple[str, str]] = {}
    for path in sorted(details_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for day in payload.get("matchSchedule", []):
            for match in day.get("matches", []):
                for player in match.get("players", []):
                    name = player.get("name") or ""
                    player_id = player.get("playerId")
                    found = ABBREV_RE.search(name)
                    if found and player_id and player_id not in players:
                        players[player_id] = (name, found.group(1).strip())
    return players


def club_from_profile(fetcher: Fetcher, player_id: str) -> tuple[int, str] | None:
    text = fetcher.get(f"{BASE_URL}/oyuncu-profil/{player_id}", CACHE_DIR / f"prof_{player_id}.html")
    links = {int(cid): clean_text(label) for cid, label in KULUP_LINK_RE.findall(text)}
    if not links:
        return None
    club_id = next(iter(links))
    return club_id, links[club_id]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incrementally resolve player club abbreviations via profiles.")
    parser.add_argument("--details-dir", type=Path, default=DETAILS_DIR)
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--jitter", type=float, default=0.7)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--refresh", action="store_true", help="Refetch profile HTML even if cached.")
    parser.add_argument("--dry-run", action="store_true", help="Report the pending work and exit.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    unique_map = load_unique_map()
    clubs = load_clubs()
    overrides = load_overrides()

    players = distinct_tagged_players(args.details_dir)
    pending: list[str] = []
    counts = {"ferdi": 0, "unique": 0, "already": 0, "pending": 0}
    for player_id, (_, abbrev) in players.items():
        if abbrev == FERDI:
            counts["ferdi"] += 1
        elif player_id in overrides:
            counts["already"] += 1
        elif abbrev in unique_map:
            counts["unique"] += 1
        else:
            counts["pending"] += 1
            pending.append(player_id)

    print(f"tagged players={len(players)} ferdi={counts['ferdi']} unique={counts['unique']} already={counts['already']} pending={counts['pending']}")
    if args.dry_run:
        for player_id in pending[:20]:
            name, abbrev = players[player_id]
            print(f"  pending {player_id} {name} ({abbrev})")
        return 0

    fetcher = Fetcher(CACHE_DIR, args.delay, args.jitter, args.timeout, args.retries, args.refresh)
    resolved = 0
    unresolved: list[tuple[str, str]] = []
    for index, player_id in enumerate(pending, start=1):
        name, abbrev = players[player_id]
        try:
            outcome = club_from_profile(fetcher, player_id)
        except Exception as exc:  # noqa: BLE001 - profile fetch is best-effort
            unresolved.append((name, f"fetch error: {exc}"))
            continue
        if outcome is None:
            unresolved.append((name, "no club link on profile"))
            continue
        club_id, club_name = outcome
        overrides[player_id] = {
            "playerId": player_id,
            "name": name,
            "abbrev": abbrev,
            "clubId": club_id,
            "clubName": club_name,
            "source": "profile",
        }
        clubs.setdefault(
            club_id,
            {"clubId": club_id, "name": club_name, "address": "", "email": "", "city": "", "phone": "", "web": "", "contact": "", "source": "profile-discovered"},
        )
        resolved += 1
        if index % 50 == 0:
            print(f"  {index}/{len(pending)} resolved...", flush=True)

    atomic_write_json(CLUBS_FILE, {"clubs": sorted(clubs.values(), key=lambda club: club["clubId"])})
    atomic_write_json(
        OVERRIDES_FILE,
        {"stats": {"resolved": len(overrides)}, "overrides": overrides},
    )

    print(f"resolved={resolved} unresolved={len(unresolved)} overrides_total={len(overrides)} clubs_total={len(clubs)}")
    for name, reason in unresolved:
        print(f"  unresolved: {name} — {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
