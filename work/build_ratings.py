"""Compute Elo ratings from completed matches and write them into tennos.db.

Single rating pool (so playing up an age group is rewarded), chronological order by
match date. Provisional K=40 for a player's first PROVISIONAL_GAMES matches, then K=20.
Only result_type='completed' matches with both winner and loser ids are rated; walkover,
retirement, bye and scheduled carry no score signal and are skipped.

Idempotent: drops and rebuilds the player_ratings table on every run.

Output table player_ratings:
    player_id, name, birth_year, age_group, club_id, club_name,
    rating, peak_rating, matches, wins, losses,
    first_match_date, last_match_date, overall_rank, age_group_rank
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "outputs" / "tennos.db"

START_RATING = 1500.0
PROVISIONAL_GAMES = 30
K_PROVISIONAL = 40.0
K_STABLE = 20.0

DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})")


def date_key(value: str | None) -> tuple[int, int, int]:
    """Sortable (year, month, day); undated matches sort last."""
    if value:
        m = DATE_RE.search(value)
        if m:
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            return (year, month, day)
    return (9999, 99, 99)


def expected(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rating_b - rating_a) / 400.0))


def k_factor(games_played: int) -> float:
    return K_PROVISIONAL if games_played < PROVISIONAL_GAMES else K_STABLE


SCHEMA = """
DROP TABLE IF EXISTS player_rating_history;
DROP TABLE IF EXISTS player_ratings;
CREATE TABLE player_ratings (
    player_id        INTEGER PRIMARY KEY,
    name             TEXT,
    birth_year       INTEGER,
    age_group        INTEGER,
    gender           TEXT,
    club_id          INTEGER,
    club_name        TEXT,
    rating           REAL,
    peak_rating      REAL,
    matches          INTEGER,
    wins             INTEGER,
    losses           INTEGER,
    first_match_date TEXT,
    last_match_date  TEXT,
    overall_rank     INTEGER,
    gender_rank      INTEGER,
    age_group_rank   INTEGER
);
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Elo ratings into tennos.db.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--top", type=int, default=20, help="How many to print.")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute(
        """
        SELECT match_id, tournament_id, match_date, age_group, gender, winner_id, loser_id
        FROM matches
        WHERE result_type='completed' AND winner_id IS NOT NULL AND loser_id IS NOT NULL
        """
    ).fetchall()
    rows.sort(key=lambda r: (date_key(r["match_date"]), r["tournament_id"] or 0, r["match_id"]))

    rating: dict[int, float] = defaultdict(lambda: START_RATING)
    peak: dict[int, float] = defaultdict(lambda: START_RATING)
    history: list[tuple] = []
    games = Counter()
    wins = Counter()
    losses = Counter()
    age_groups: dict[int, Counter] = defaultdict(Counter)
    last_age_group: dict[int, int] = {}
    genders_seen: dict[int, Counter] = defaultdict(Counter)
    first_date: dict[int, str] = {}
    last_date: dict[int, str] = {}

    for r in rows:
        w, l = r["winner_id"], r["loser_id"]
        rw, rl = rating[w], rating[l]
        ew = expected(rw, rl)
        rating[w] = rw + k_factor(games[w]) * (1.0 - ew)
        rating[l] = rl + k_factor(games[l]) * (0.0 - (1.0 - ew))
        for pid in (w, l):
            games[pid] += 1
            peak[pid] = max(peak[pid], rating[pid])
            md = r["match_date"]
            if md:
                first_date.setdefault(pid, md)
                last_date[pid] = md
            if r["age_group"]:
                age_groups[pid][r["age_group"]] += 1
                last_age_group[pid] = r["age_group"]
            if r["gender"]:
                genders_seen[pid][r["gender"]] += 1
        wins[w] += 1
        losses[l] += 1
        md = r["match_date"]
        history.append((w, md, round(rating[w], 1), l, 1))
        history.append((l, md, round(rating[l], 1), w, 0))

    # player metadata
    meta = {
        row["player_id"]: row
        for row in cur.execute("SELECT player_id, name, birth_year, gender, club_id, club_name FROM players").fetchall()
    }

    records = []
    for pid in rating:
        info = meta.get(pid)
        primary_age = max(age_groups[pid]) if age_groups[pid] else None  # highest category played
        gender = (info["gender"] if info and info["gender"] else None) or (
            genders_seen[pid].most_common(1)[0][0] if genders_seen[pid] else None
        )
        records.append(
            {
                "player_id": pid,
                "name": info["name"] if info else None,
                "birth_year": info["birth_year"] if info else None,
                "age_group": primary_age,
                "gender": gender,
                "club_id": info["club_id"] if info else None,
                "club_name": info["club_name"] if info else None,
                "rating": round(rating[pid], 1),
                "peak_rating": round(peak[pid], 1),
                "matches": games[pid],
                "wins": wins[pid],
                "losses": losses[pid],
                "first_match_date": first_date.get(pid),
                "last_match_date": last_date.get(pid),
            }
        )

    records.sort(key=lambda x: x["rating"], reverse=True)
    gender_counter: Counter = Counter()
    cat_counter: Counter = Counter()  # (gender, age_group)
    for index, rec in enumerate(records, start=1):  # already sorted by rating desc
        rec["overall_rank"] = index
        g = rec["gender"]
        gender_counter[g] += 1
        rec["gender_rank"] = gender_counter[g]
        cat_counter[(g, rec["age_group"])] += 1
        rec["age_group_rank"] = cat_counter[(g, rec["age_group"])]

    cur.executescript(SCHEMA)
    cur.executemany(
        """INSERT INTO player_ratings VALUES
        (:player_id,:name,:birth_year,:age_group,:gender,:club_id,:club_name,:rating,:peak_rating,
         :matches,:wins,:losses,:first_match_date,:last_match_date,:overall_rank,:gender_rank,:age_group_rank)""",
        records,
    )
    cur.executescript(
        "CREATE INDEX idx_ratings_cat ON player_ratings(gender, age_group, age_group_rank);"
        "CREATE INDEX idx_ratings_gender ON player_ratings(gender, gender_rank);"
        "CREATE INDEX idx_ratings_rating ON player_ratings(rating DESC);"
    )
    cur.execute(
        "CREATE TABLE player_rating_history (player_id INTEGER NOT NULL, match_date TEXT, rating_after REAL NOT NULL, opponent_id INTEGER, won INTEGER)"
    )
    cur.executemany(
        "INSERT INTO player_rating_history VALUES (?,?,?,?,?)", history
    )
    cur.execute("CREATE INDEX idx_rh_player ON player_rating_history(player_id)")
    conn.commit()

    print(f"rated players={len(records)} matches_used={len(rows)}")
    print(f"\nTop {args.top} (genel):")
    for rec in records[: args.top]:
        print(
            f"  {rec['overall_rank']:3} {rec['rating']:6.0f}  {(rec['name'] or '?')[:26]:26} "
            f"{(str(rec['birth_year']) if rec['birth_year'] else '?'):4} "
            f"{rec['wins']:3}-{rec['losses']:<3} {(rec['club_name'] or '')[:22]}"
        )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
