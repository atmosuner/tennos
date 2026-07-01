"""Produce slimmed copies of tennos.db for shipping to the browser (sql.js).

Drops what the web frontend never queries: the match_players and groups tables,
and the large matches.raw_text column. Also drops the non-PK indexes — they're
~35% of the file and the frontend rebuilds them client-side after load (see
REBUILD_INDEX_SQL in index.html's bootDB, which must be kept in sync with this
list).

Then splits into two files so a first visit (home page) doesn't have to download
match-level data it never queries:

- tennos-web.db        "core": clubs, players, tournaments, player_ratings,
                        web_precomputed (home page's stats/homeInsights, computed
                        here once instead of via heavy correlated subqueries in
                        the browser). Fetched eagerly on every page load.
- tennos-web-detail.db  everything else (matches, sets, klasman_puan,
                        player_rating_history) PLUS a copy of the core tables so
                        it's self-contained (sql.js has no ATTACH-from-bytes
                        support, so cross-file joins aren't possible — pages that
                        need detail data query this file exclusively instead).
                        Fetched lazily by ensureDetail() in index.html on first
                        navigation to a page that needs it (rankings, player
                        profile, tournament detail, h2h, compare).

    python3 work/build_web_db.py   ->  web/tennos-web.db + web/tennos-web-detail.db
"""

from __future__ import annotations

import calendar
import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "outputs" / "tennos.db"
WEB_DIR = Path(__file__).resolve().parent / "web"
DST_FULL = WEB_DIR / "tennos-web-detail.db"
DST_CORE = WEB_DIR / "tennos-web.db"

CORE_TABLES = ("clubs", "players", "tournaments", "player_ratings", "web_precomputed")
DETAIL_ONLY_TABLES = ("matches", "sets", "klasman_puan", "player_rating_history")

MONTHS = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
          "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def months_ago_str(n: int) -> str:
    d = date.today()
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12
        y -= 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day).strftime("%Y%m%d")


def rows_to_dicts(cur: sqlite3.Cursor) -> list[dict]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def compute_stats(conn: sqlite3.Connection) -> dict:
    """Mirrors ep.stats() in index.html — same SQL, ported to Python."""
    cur = conn.cursor()
    cs = months_ago_str(6)
    af = (
        f"SUBSTR(last_match_date,7,4)||SUBSTR(last_match_date,4,2)||"
        f"SUBSTR(last_match_date,1,2)>='{cs}'"
    )
    by_age_gender = rows_to_dicts(cur.execute(
        f"SELECT age_group,gender,count(*) n FROM player_ratings "
        f"WHERE age_group IS NOT NULL AND gender IS NOT NULL AND {af} "
        f"GROUP BY age_group,gender ORDER BY age_group,gender"
    ))
    for c in by_age_gender:
        c["top3"] = rows_to_dicts(cur.execute(
            f"SELECT player_id,name,rating,club_name FROM player_ratings "
            f"WHERE age_group=? AND gender=? AND {af} ORDER BY rating DESC LIMIT 3",
            (c["age_group"], c["gender"]),
        ))
    scalar = lambda sql, p=(): cur.execute(sql, p).fetchone()[0]
    return {
        "players": scalar("SELECT count(*) FROM players"),
        "ratedPlayers": scalar("SELECT count(*) FROM player_ratings"),
        "activeRatedPlayers": scalar("SELECT count(*) FROM player_ratings"),
        "clubs": scalar("SELECT count(*) FROM clubs"),
        "tournaments": scalar("SELECT count(*) FROM tournaments"),
        "matches": scalar("SELECT count(*) FROM matches"),
        "completed": scalar("SELECT count(*) FROM matches WHERE result_type='completed'"),
        "byAgeGender": by_age_gender,
    }


def compute_home_insights(conn: sqlite3.Connection) -> dict:
    """Mirrors ep.homeInsights() in index.html — same SQL, ported to Python."""
    cur = conn.cursor()
    acs = months_ago_str(6)
    af = lambda p="": (
        f"SUBSTR({p}last_match_date,7,4)||SUBSTR({p}last_match_date,4,2)||"
        f"SUBSTR({p}last_match_date,1,2)>='{acs}'"
    )
    one = lambda sql, p=(): (rows_to_dicts(cur.execute(sql, p)) or [None])[0]
    scalar = lambda sql, p=(): cur.execute(sql, p).fetchone()[0]

    year = scalar("SELECT MAX(SUBSTR(match_date,7,4)) FROM matches WHERE match_date<>''") or ""
    yf = "SUBSTR(match_date,7,4)=?"
    busiest = one(
        f"SELECT SUBSTR(match_date,4,2) mo,count(*) n FROM matches WHERE {yf} "
        f"AND match_date<>'' GROUP BY mo ORDER BY n DESC LIMIT 1",
        (year,),
    )
    top_cat = one(
        f"SELECT age_group,gender,count(*) n FROM matches WHERE {yf} "
        f"AND age_group IS NOT NULL AND gender<>'' GROUP BY age_group,gender ORDER BY n DESC LIMIT 1",
        (year,),
    )
    season = {
        "year": year,
        "matches": scalar(f"SELECT count(*) FROM matches WHERE {yf}", (year,)),
        "completed": scalar(f"SELECT count(*) FROM matches WHERE {yf} AND result_type='completed'", (year,)),
        "tournaments": scalar("SELECT count(*) FROM tournaments WHERE year=?", (int(year) if year else None,)),
        "busiestMonth": MONTHS[int(busiest["mo"])] if busiest else "—",
        "topCategory": f"{top_cat['age_group']} Yaş {top_cat['gender']}" if top_cat else "—",
    }

    biggest_rise = one(
        """
        WITH h AS (SELECT player_id,rating_after,
                          SUBSTR(match_date,7,4)||SUBSTR(match_date,4,2)||SUBSTR(match_date,1,2) dk
                   FROM player_rating_history),
             strt AS (SELECT player_id,count(*) c,MIN(dk) mindk FROM h GROUP BY player_id)
        SELECT pr.player_id,pr.name,
               CAST(round(pr.rating-(SELECT rating_after FROM h WHERE h.player_id=strt.player_id
                                      AND h.dk=strt.mindk LIMIT 1)) AS INTEGER) v
        FROM strt JOIN player_ratings pr ON pr.player_id=strt.player_id
        WHERE strt.c>=5 ORDER BY v DESC LIMIT 1
        """
    )
    if biggest_rise and biggest_rise.get("v") is not None:
        v = biggest_rise["v"]
        biggest_rise["v"] = ("+" if v > 0 else "") + str(v)

    records = {
        "peak": one("SELECT player_id,name,round(peak_rating) v FROM player_ratings ORDER BY peak_rating DESC LIMIT 1"),
        "mostMatches": one("SELECT player_id,name,matches v FROM player_ratings ORDER BY matches DESC LIMIT 1"),
        "youngest": one(
            "SELECT player_id,name,birth_year v,round(rating) rating FROM player_ratings "
            "WHERE matches>=5 AND birth_year IS NOT NULL ORDER BY birth_year DESC,rating DESC LIMIT 1"
        ),
        "bestPct": one(
            "SELECT player_id,name,wins,losses,round(wins*100.0/matches) v FROM player_ratings "
            "WHERE matches>=30 ORDER BY wins*1.0/matches DESC LIMIT 1"
        ),
        "mostTournaments": one(
            """
            WITH pt AS (
                SELECT player_id,count(DISTINCT tournament_id) v FROM (
                    SELECT winner_id player_id,tournament_id FROM matches WHERE winner_id IS NOT NULL AND tournament_id IS NOT NULL
                    UNION ALL
                    SELECT loser_id player_id,tournament_id FROM matches WHERE loser_id IS NOT NULL AND tournament_id IS NOT NULL
                ) GROUP BY player_id
            )
            SELECT pr.player_id,pr.name,pt.v FROM player_ratings pr JOIN pt ON pt.player_id=pr.player_id
            ORDER BY pt.v DESC LIMIT 1
            """
        ),
        "biggestRise": biggest_rise,
    }

    top_clubs = rows_to_dicts(cur.execute(
        f"""
        SELECT club_id,club_name,sum(wins) w,sum(losses) l,
               round(sum(wins)*100.0/(sum(wins)+sum(losses))) pct,count(*) n
        FROM player_ratings WHERE club_id IS NOT NULL AND club_name<>'' AND matches>=5 AND {af()}
        GROUP BY club_id HAVING n>=20 AND (w+l)>0 ORDER BY pct DESC LIMIT 8
        """
    ))
    max_birth = scalar(f"SELECT MAX(birth_year) FROM player_ratings WHERE matches>=5 AND {af()}")
    young_talents = rows_to_dicts(cur.execute(
        f"""
        SELECT player_id,name,birth_year,rating,club_name FROM player_ratings
        WHERE birth_year>=? AND matches>=5 AND {af()} ORDER BY rating DESC LIMIT 10
        """,
        ((max_birth or 0) - 1,),
    ))
    upsets = rows_to_dicts(cur.execute(
        f"""
        SELECT m.winner_id,wr.name wn,round(wr.rating) wrating,m.loser_id,lr.name ln,round(lr.rating) lrating,
               round(lr.rating-wr.rating) gap,m.match_date,m.event
        FROM matches m JOIN player_ratings wr ON wr.player_id=m.winner_id JOIN player_ratings lr ON lr.player_id=m.loser_id
        WHERE m.result_type='completed' AND wr.matches>=10 AND lr.matches>=10
              AND {af('wr.')} AND {af('lr.')} AND lr.rating-wr.rating>0
        ORDER BY gap DESC LIMIT 6
        """
    ))
    cohorts = rows_to_dicts(cur.execute(
        f"""
        SELECT birth_year,round(avg(rating)) avg,count(*) n FROM player_ratings
        WHERE birth_year IS NOT NULL AND matches>=5 AND {af()} GROUP BY birth_year HAVING n>=20
        ORDER BY birth_year DESC LIMIT 8
        """
    ))
    cities = rows_to_dicts(cur.execute(
        """
        SELECT p.city,count(*) n FROM player_ratings pr JOIN players p ON p.player_id=pr.player_id
        WHERE p.city<>'' AND p.city IS NOT NULL GROUP BY p.city ORDER BY n DESC LIMIT 10
        """
    ))
    return {
        "season": season, "records": records, "topClubs": top_clubs,
        "youngTalents": young_talents, "upsets": upsets, "cohorts": cohorts, "cities": cities,
    }


def write_web_precomputed(conn: sqlite3.Connection) -> None:
    stats = compute_stats(conn)
    insights = compute_home_insights(conn)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS web_precomputed")
    cur.execute("CREATE TABLE web_precomputed (key TEXT PRIMARY KEY, json TEXT)")
    cur.executemany(
        "INSERT INTO web_precomputed VALUES (?,?)",
        [("stats", json.dumps(stats)), ("homeInsights", json.dumps(insights))],
    )
    conn.commit()


def strip_indexes(cur: sqlite3.Cursor) -> None:
    idx_names = [
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_autoindex%'"
        ).fetchall()
    ]
    for name in idx_names:
        cur.execute(f'DROP INDEX "{name}"')


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"DB yok: {SRC}")
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, DST_FULL)
    conn = sqlite3.connect(DST_FULL)
    cur = conn.cursor()
    cur.executescript("DROP TABLE IF EXISTS match_players; DROP TABLE IF EXISTS groups;")
    cols = [r[1] for r in cur.execute("PRAGMA table_info(matches)").fetchall()]
    if "raw_text" in cols:
        cur.execute("ALTER TABLE matches DROP COLUMN raw_text")
    strip_indexes(cur)
    # Denormalize dominant age_group onto tournaments so the core DB can filter/list
    # without touching the matches table (which only lives in the detail DB).
    cols_t = [r[1] for r in cur.execute("PRAGMA table_info(tournaments)").fetchall()]
    if "age_group" not in cols_t:
        cur.execute("ALTER TABLE tournaments ADD COLUMN age_group INTEGER")
    cur.execute("""
        UPDATE tournaments SET age_group = (
            SELECT age_group FROM matches
            WHERE tournament_id = tournaments.tournament_id AND age_group IS NOT NULL
            GROUP BY age_group ORDER BY count(*) DESC LIMIT 1
        )
    """)
    conn.commit()
    write_web_precomputed(conn)  # needs matches/player_rating_history, so before the core copy splits them off
    conn.commit()

    # tennos-web.db (core): copy of the slimmed full db, then drop the detail-only tables.
    shutil.copyfile(DST_FULL, DST_CORE)
    core_conn = sqlite3.connect(DST_CORE)
    core_cur = core_conn.cursor()
    for t in DETAIL_ONLY_TABLES:
        core_cur.execute(f'DROP TABLE IF EXISTS "{t}"')
    core_conn.commit()
    core_conn.execute("VACUUM")
    core_conn.close()

    # tennos-web-detail.db (full): keep as-is, self-contained (core tables + detail tables).
    conn.execute("VACUUM")
    conn.close()

    core_mb = DST_CORE.stat().st_size / 1e6
    full_mb = DST_FULL.stat().st_size / 1e6
    print(f"core db -> {DST_CORE} ({core_mb:.1f} MB)")
    print(f"detail db -> {DST_FULL} ({full_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
