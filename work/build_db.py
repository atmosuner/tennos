"""Build a SQLite database from the scraped JSON outputs.

Single source of truth is the outputs/ directory; the database is a derived view.
The build is idempotent: every run drops and recreates all tables, so re-running after
the scraper adds tournaments simply reflects the current JSON state.

Inputs:
    outputs/tournament_details/*.json   tournaments, matches, sets, groups
    outputs/clubs.json                  club master (539+)
    outputs/players.json                player directory (30.9k)
    outputs/player_club_overrides.json  resolved match abbreviation -> club
    outputs/club_abbrev_map.json        trusted unique abbreviation -> club

Output:
    outputs/tennos.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = PROJECT_ROOT / "outputs"
DETAILS_DIR = OUTPUTS / "tournament_details"
DB_PATH = OUTPUTS / "tennos.db"

EVENT_AGE_RE = re.compile(r"(\d+)\s*Yaş", re.I)
GENDER_RE = re.compile(r"\b(Erkek|Kadın|Kız)\b", re.I)

SCHEMA = """
DROP TABLE IF EXISTS tournaments;
DROP TABLE IF EXISTS clubs;
DROP TABLE IF EXISTS players;
DROP TABLE IF EXISTS matches;
DROP TABLE IF EXISTS match_players;
DROP TABLE IF EXISTS sets;
DROP TABLE IF EXISTS groups;

CREATE TABLE tournaments (
    tournament_id  INTEGER PRIMARY KEY,
    name           TEXT,
    title          TEXT,
    type_text      TEXT,
    category       TEXT,
    city           TEXT,
    surface        TEXT,
    court_type     TEXT,
    region_type    TEXT,
    series_type    TEXT,
    court_count    INTEGER,
    club_id        INTEGER,
    head_referee   TEXT,
    start_date     TEXT,
    end_date       TEXT,
    reg_open_date  TEXT,
    reg_close_date TEXT,
    withdraw_date  TEXT,
    week           INTEGER,
    year           INTEGER,
    source_tab     TEXT,
    image_url      TEXT,
    scraped_at     TEXT
);

CREATE TABLE clubs (
    club_id  INTEGER PRIMARY KEY,
    name     TEXT,
    city     TEXT,
    address  TEXT,
    email    TEXT,
    phone    TEXT,
    web      TEXT,
    contact  TEXT,
    source   TEXT,
    abbrev   TEXT
);

CREATE TABLE players (
    player_id   INTEGER PRIMARY KEY,
    name        TEXT,
    birth_year  INTEGER,
    gender      TEXT,
    club_id     INTEGER,
    club_name   TEXT,
    city        TEXT,
    profile_url TEXT
);

CREATE TABLE matches (
    match_id      TEXT PRIMARY KEY,
    tournament_id INTEGER,
    day_id        INTEGER,
    day_name      TEXT,
    match_date    TEXT,
    court         TEXT,
    match_code    TEXT,
    start_time    TEXT,
    event         TEXT,
    age_group     INTEGER,
    gender        TEXT,
    stage         TEXT,
    is_double     INTEGER,
    result_type   TEXT,
    winner_id     INTEGER,
    loser_id      INTEGER,
    p1_id         INTEGER,
    p2_id         INTEGER,
    raw_text      TEXT
);

CREATE TABLE match_players (
    match_id    TEXT,
    player_id   INTEGER,
    name        TEXT,
    club_abbrev TEXT,
    club_id     INTEGER,
    is_winner   INTEGER,
    status      TEXT
);

CREATE TABLE sets (
    match_id    TEXT,
    set_number  INTEGER,
    set_type    TEXT,
    p1_games    INTEGER,
    p1_tiebreak INTEGER,
    p2_games    INTEGER,
    p2_tiebreak INTEGER
);

CREATE TABLE groups (
    group_id          INTEGER PRIMARY KEY,
    tournament_id     INTEGER,
    name              TEXT,
    participant_count INTEGER,
    fixture_url       TEXT
);
"""

INDEXES = """
CREATE INDEX idx_match_tournament ON matches(tournament_id);
CREATE INDEX idx_match_event ON matches(age_group, gender);
CREATE INDEX idx_match_winner ON matches(winner_id);
CREATE INDEX idx_match_loser ON matches(loser_id);
CREATE INDEX idx_mp_player ON match_players(player_id);
CREATE INDEX idx_mp_match ON match_players(match_id);
CREATE INDEX idx_sets_match ON sets(match_id);
CREATE INDEX idx_players_club ON players(club_id);
"""

ABBREV_RE = re.compile(r"\(([^)]+)\)\s*$")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def match_uid(tournament_id: str, day_id: Any, court: Any, match_code: Any, raw_text: str) -> str:
    key = f"{tournament_id}|{day_id}|{court}|{match_code}|{raw_text}"
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:16]


def derive_event(event: str) -> tuple[int | None, str | None]:
    age = EVENT_AGE_RE.search(event or "")
    gender = GENDER_RE.search(event or "")
    gender_value = gender.group(1).title() if gender else None
    if gender_value == "Kız":
        gender_value = "Kadın"
    return (int(age.group(1)) if age else None, gender_value)


def insert_clubs(cur: sqlite3.Cursor) -> int:
    clubs = load_json(OUTPUTS / "clubs.json", {"clubs": []})["clubs"]
    cur.executemany(
        "INSERT OR REPLACE INTO clubs VALUES (:club_id,:name,:city,:address,:email,:phone,:web,:contact,:source,:abbrev)",
        [
            {
                "club_id": c["clubId"],
                "name": c.get("name"),
                "city": c.get("city"),
                "address": c.get("address"),
                "email": c.get("email"),
                "phone": c.get("phone"),
                "web": c.get("web"),
                "contact": c.get("contact"),
                "source": c.get("source", "kulupler-list"),
                "abbrev": None,
            }
            for c in clubs
        ],
    )
    return len(clubs)


def populate_club_abbrev(cur: sqlite3.Cursor, unique_map: dict[str, int]) -> int:
    """Fill clubs.abbrev: trusted unique abbrev first, else the most common
    non-FERDI abbrev seen in match_players for that club."""
    for abbrev, club_id in unique_map.items():
        cur.execute("UPDATE clubs SET abbrev=? WHERE club_id=? AND abbrev IS NULL", (abbrev, club_id))
    rows = cur.execute(
        """
        SELECT club_id, club_abbrev, count(*) c FROM match_players
        WHERE club_id IS NOT NULL AND club_abbrev IS NOT NULL AND club_abbrev<>'' AND club_abbrev<>'FERDI'
        GROUP BY club_id, club_abbrev
        """
    ).fetchall()
    best: dict[int, tuple[str, int]] = {}
    for club_id, abbrev, c in rows:
        if club_id not in best or c > best[club_id][1]:
            best[club_id] = (abbrev, c)
    cur.executemany(
        "UPDATE clubs SET abbrev=? WHERE club_id=? AND abbrev IS NULL",
        [(abbrev, club_id) for club_id, (abbrev, _) in best.items()],
    )
    return cur.execute("SELECT count(*) FROM clubs WHERE abbrev IS NOT NULL").fetchone()[0]


def insert_players(cur: sqlite3.Cursor) -> int:
    players = load_json(OUTPUTS / "players.json", {"players": []})["players"]
    cur.executemany(
        "INSERT OR REPLACE INTO players VALUES (:player_id,:name,:birth_year,:gender,:club_id,:club_name,:city,:profile_url)",
        [
            {
                "player_id": p["playerId"],
                "name": p.get("name"),
                "birth_year": p.get("birthYear"),
                "gender": p.get("gender"),
                "club_id": p.get("clubId"),
                "club_name": p.get("clubName"),
                "city": p.get("city"),
                "profile_url": p.get("profileUrl"),
            }
            for p in players
        ],
    )
    return len(players)


def backfill_missing_players(cur: sqlite3.Cursor) -> int:
    """Players who only appear in old tournaments (pre-dating the current player
    directory) are missing from players.json. Recover their name/club from the
    match data itself so player_ratings.name isn't NULL."""
    cur.execute(
        """
        SELECT mp.player_id, mp.name, mp.club_id, COUNT(*) c
        FROM match_players mp
        WHERE mp.player_id IS NOT NULL
          AND mp.player_id NOT IN (SELECT player_id FROM players)
        GROUP BY mp.player_id, mp.name, mp.club_id
        """
    )
    best: dict[int, tuple[str | None, int | None, int]] = {}
    for pid, name, club_id, count in cur.fetchall():
        if pid not in best or count > best[pid][2]:
            best[pid] = (name, club_id, count)
    rows = [
        {
            "player_id": pid,
            "name": ABBREV_RE.sub("", name or "").strip() or None,
            "club_id": club_id,
        }
        for pid, (name, club_id, _count) in best.items()
    ]
    if rows:
        cur.executemany(
            "INSERT OR IGNORE INTO players (player_id, name, club_id) VALUES (:player_id, :name, :club_id)",
            rows,
        )
        cur.execute(
            "UPDATE players SET club_name=(SELECT name FROM clubs WHERE clubs.club_id=players.club_id) "
            "WHERE club_name IS NULL AND club_id IS NOT NULL"
        )
    return len(rows)


def resolve_club(player_id: Any, abbrev: str | None, overrides: dict[str, Any], unique_map: dict[str, int]) -> int | None:
    if player_id is not None and str(player_id) in overrides:
        return overrides[str(player_id)].get("clubId")
    if abbrev and abbrev != "FERDI":
        return unique_map.get(abbrev)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Build outputs/tennos.db from scraped JSON.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    args = parser.parse_args()

    overrides = load_json(OUTPUTS / "player_club_overrides.json", {"overrides": {}})["overrides"]
    unique_map = {
        e["abbrev"]: e["clubId"]
        for e in load_json(OUTPUTS / "club_abbrev_map.json", {"map": []})["map"]
        if e["status"] == "unique"
    }

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    n_clubs = insert_clubs(cur)
    n_players = insert_players(cur)

    n_tournaments = n_matches = n_match_players = n_sets = n_groups = 0
    seen_groups: set[int] = set()

    for path in sorted(DETAILS_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        tour = payload.get("tournament", {})
        fields = tour.get("fields", {})
        entry = payload.get("source", {}).get("inputEntry", {})
        club = fields.get("kulup") if isinstance(fields.get("kulup"), dict) else {}
        tid = int_or_none(tour.get("turnuvaId"))
        cur.execute(
            "INSERT OR REPLACE INTO tournaments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid, tour.get("name"), tour.get("title"), tour.get("typeText"), fields.get("kategori"),
                fields.get("yer"), fields.get("zemin"), fields.get("kortTipi"), fields.get("bolgeTipi"),
                fields.get("seriTipi"), int_or_none(fields.get("kortSayisi")),
                int_or_none(club.get("id")), fields.get("bashakem") if isinstance(fields.get("bashakem"), str) else None,
                fields.get("baslangicTarihi"), fields.get("bitisTarihi"), fields.get("kayitKabulBaslangic"),
                fields.get("sonKayitTarihi"), fields.get("geriCekilme"),
                int_or_none(entry.get("hafta")), int_or_none(entry.get("year")), entry.get("tab"),
                tour.get("imageUrl"), payload.get("scrapedAt"),
            ),
        )
        n_tournaments += 1

        for group in payload.get("groups", []):
            gid = int_or_none(group.get("groupId"))
            if gid is None or gid in seen_groups:
                continue
            seen_groups.add(gid)
            cur.execute(
                "INSERT OR REPLACE INTO groups VALUES (?,?,?,?,?)",
                (gid, tid, group.get("name"), int_or_none(group.get("participantCount")), group.get("fixtureUrl")),
            )
            n_groups += 1

        for day in payload.get("matchSchedule", []):
            for m in day.get("matches", []):
                mid = match_uid(str(tid), m.get("dayId"), m.get("court"), m.get("matchCode"), m.get("rawText", ""))
                age_group, gender = derive_event(m.get("event", ""))
                result = m.get("result", {})
                mplayers = m.get("players", [])
                p1_id = int_or_none(mplayers[0].get("playerId")) if len(mplayers) > 0 else None
                p2_id = int_or_none(mplayers[1].get("playerId")) if len(mplayers) > 1 else None
                cur.execute(
                    "INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        mid, tid, int_or_none(m.get("dayId")), m.get("dayName"), m.get("date"),
                        m.get("court"), m.get("matchCode"), m.get("startTime"), m.get("event"),
                        age_group, gender, m.get("stage"), 1 if m.get("isDouble") else 0,
                        result.get("type"),
                        int_or_none((result.get("winner") or {}).get("playerId")),
                        int_or_none((result.get("loser") or {}).get("playerId")),
                        p1_id, p2_id,
                        m.get("rawText"),
                    ),
                )
                n_matches += 1

                for player in m.get("players", []):
                    name = player.get("name") or ""
                    found = ABBREV_RE.search(name)
                    abbrev = found.group(1).strip() if found else None
                    pid = int_or_none(player.get("playerId"))
                    club_id = resolve_club(pid, abbrev, overrides, unique_map)
                    cur.execute(
                        "INSERT INTO match_players VALUES (?,?,?,?,?,?,?)",
                        (
                            mid, pid, name, abbrev, club_id,
                            1 if player.get("isWinner") else (0 if player.get("isWinner") is False else None),
                            player.get("status"),
                        ),
                    )
                    n_match_players += 1

                for s in m.get("sets", []):
                    p1, p2 = s.get("p1") or {}, s.get("p2") or {}
                    cur.execute(
                        "INSERT INTO sets VALUES (?,?,?,?,?,?,?)",
                        (
                            mid, s.get("setNumber"), s.get("type"),
                            int_or_none(p1.get("games")), int_or_none(p1.get("tiebreak")),
                            int_or_none(p2.get("games")), int_or_none(p2.get("tiebreak")),
                        ),
                    )
                    n_sets += 1

    n_backfilled_players = backfill_missing_players(cur)
    n_club_abbrev = populate_club_abbrev(cur, unique_map)

    cur.executescript(INDEXES)
    conn.commit()
    conn.close()

    print(f"db={args.db}")
    print(f"clubs={n_clubs} (+{n_club_abbrev} with abbrev) players={n_players} (+{n_backfilled_players} backfilled from match data) tournaments={n_tournaments} groups={n_groups}")
    print(f"matches={n_matches} match_players={n_match_players} sets={n_sets}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
