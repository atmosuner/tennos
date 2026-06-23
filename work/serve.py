"""Read-only JSON API + static frontend for tennos.db, using only the standard library.

Run:  python3 work/serve.py            (serves on http://localhost:8001)

Endpoints:
    GET /api/stats                       overview counts
    GET /api/rankings?age_group=&gender=&q=&club_id=&limit=&offset=
    GET /api/player/{id}                 profile, record, recent matches, top opponents
    GET /api/h2h/{a}/{b}                 head-to-head match list
    GET /api/clubs?q=&limit=             clubs with player/match counts
    GET /api/search?q=                   players + clubs by name
Everything else is served from work/web/ (index.html is the SPA).
"""

from __future__ import annotations

import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "outputs" / "tennos.db"
WEB_DIR = Path(__file__).resolve().parent / "web"
PORT = 8001


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


# Turkish-aware, case-insensitive search folding (works in SQLite + sql.js identically).
_TR_FOLD = str.maketrans("ıİşŞğĞçÇöÖüÜ", "IISSGGCCOOUU")
_FOLD_PAIRS = [("ı", "I"), ("İ", "I"), ("ş", "S"), ("Ş", "S"), ("ğ", "G"), ("Ğ", "G"),
               ("ç", "C"), ("Ç", "C"), ("ö", "O"), ("Ö", "O"), ("ü", "U"), ("Ü", "U")]


def fold(s: str | None) -> str:
    return (s or "").translate(_TR_FOLD).upper()


def foldsql(col: str) -> str:
    expr = col
    for a, b in _FOLD_PAIRS:
        expr = f"REPLACE({expr},'{a}','{b}')"
    return f"UPPER({expr})"


def build_score(conn: sqlite3.Connection, match_id: str, player_is_first: bool) -> str:
    """Render a match score from the player's perspective (player games - opponent games)."""
    sets = conn.execute(
        "SELECT set_number, p1_games, p1_tiebreak, p2_games, p2_tiebreak FROM sets WHERE match_id=? ORDER BY set_number",
        (match_id,),
    ).fetchall()
    parts = []
    for s in sets:
        a, b = (s["p1_games"], s["p2_games"]) if player_is_first else (s["p2_games"], s["p1_games"])
        ta, tb = (s["p1_tiebreak"], s["p2_tiebreak"]) if player_is_first else (s["p2_tiebreak"], s["p1_tiebreak"])
        if a is None and b is None:
            continue
        piece = f"{a if a is not None else '-'}-{b if b is not None else '-'}"
        if ta is not None or tb is not None:
            piece += f"({ta if ta is not None else 0}-{tb if tb is not None else 0})"
        parts.append(piece)
    return " ".join(parts)


def build_set_cols(conn: sqlite3.Connection, match_id: str, player_is_first: bool) -> list[dict]:
    """Per-set games/tiebreak from the player's perspective, for column-style score display."""
    sets = conn.execute(
        "SELECT set_number, p1_games, p1_tiebreak, p2_games, p2_tiebreak FROM sets WHERE match_id=? ORDER BY set_number",
        (match_id,),
    ).fetchall()
    out = []
    for s in sets:
        w, l = (s["p1_games"], s["p2_games"]) if player_is_first else (s["p2_games"], s["p1_games"])
        tw, tl = (s["p1_tiebreak"], s["p2_tiebreak"]) if player_is_first else (s["p2_tiebreak"], s["p1_tiebreak"])
        if w is None and l is None:
            continue
        out.append({"w": w, "l": l, "tw": tw, "tl": tl})
    return out


def build_set_score(conn: sqlite3.Connection, match_id: str, first_is_winner: bool) -> str:
    """Set count from the winner's perspective, e.g. '2-1'."""
    sets = conn.execute(
        "SELECT p1_games, p2_games FROM sets WHERE match_id=? ORDER BY set_number", (match_id,)
    ).fetchall()
    w = l = 0
    for s in sets:
        wg, lg = (s["p1_games"], s["p2_games"]) if first_is_winner else (s["p2_games"], s["p1_games"])
        if wg is None and lg is None:
            continue
        if (wg or 0) > (lg or 0):
            w += 1
        elif (lg or 0) > (wg or 0):
            l += 1
    return f"{w}-{l}" if (w + l) else ""


def player_matches(conn: sqlite3.Connection, player_id: int, limit: int | None = None) -> list[dict]:
    sql = """
        SELECT m.match_id, m.match_date, m.event, m.stage, m.result_type,
               m.winner_id, m.loser_id, m.p1_id, m.tournament_id, t.name AS tournament_name, t.city
        FROM matches m LEFT JOIN tournaments t ON t.tournament_id=m.tournament_id
        WHERE (m.winner_id=? OR m.loser_id=?) AND m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL
    """
    rows = conn.execute(sql, (player_id, player_id)).fetchall()
    # chronological desc by date string components
    def key(r):
        mm = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", r["match_date"] or "")
        return (int(mm.group(3)), int(mm.group(2)), int(mm.group(1))) if mm else (0, 0, 0)
    rows.sort(key=key, reverse=True)
    out = []
    names = {}
    for r in rows:
        won = r["winner_id"] == player_id
        opp_id = r["loser_id"] if won else r["winner_id"]
        if opp_id not in names:
            nm = conn.execute("SELECT name FROM players WHERE player_id=?", (opp_id,)).fetchone()
            names[opp_id] = nm["name"] if nm else f"#{opp_id}"
        out.append(
            {
                "matchId": r["match_id"],
                "date": r["match_date"],
                "event": r["event"],
                "stage": r["stage"],
                "won": won,
                "opponentId": opp_id,
                "opponentName": names[opp_id],
                "score": build_score(conn, r["match_id"], player_is_first=(r["p1_id"] == player_id)),
                "tournamentId": r["tournament_id"],
                "tournamentName": r["tournament_name"],
                "city": r["city"],
            }
        )
    return out[:limit] if limit else out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(path.suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                self.route_api(path, q)
            elif path == "/" or not path.startswith("/"):
                self.send_file(WEB_DIR / "index.html")
            else:
                candidate = (WEB_DIR / path.lstrip("/")).resolve()
                if WEB_DIR in candidate.parents or candidate == WEB_DIR:
                    self.send_file(candidate)
                else:
                    self.send_file(WEB_DIR / "index.html")
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": str(exc)}, status=500)

    def route_api(self, path: str, q: dict):
        conn = db()
        try:
            if path == "/api/stats":
                self.send_json(self.api_stats(conn))
            elif path == "/api/homeInsights":
                self.send_json(self.api_home_insights(conn))
            elif path == "/api/rankings":
                self.send_json(self.api_rankings(conn, q))
            elif path.startswith("/api/player/"):
                self.send_json(self.api_player(conn, int(path.rsplit("/", 1)[1])))
            elif path.startswith("/api/h2h/"):
                a, b = path.split("/")[3:5]
                self.send_json(self.api_h2h(conn, int(a), int(b)))
            elif path.startswith("/api/common/"):
                a, b = path.split("/")[3:5]
                self.send_json(self.api_common(conn, int(a), int(b)))
            elif path == "/api/players":
                self.send_json(self.api_players(conn, q))
            elif path == "/api/tournaments":
                self.send_json(self.api_tournaments(conn, q))
            elif path.startswith("/api/tournament/"):
                self.send_json(self.api_tournament(conn, int(path.rsplit("/", 1)[1])))
            elif path == "/api/clubs":
                self.send_json(self.api_clubs(conn, q))
            elif path.startswith("/api/club/"):
                self.send_json(self.api_club(conn, int(path.rsplit("/", 1)[1])))
            elif path == "/api/search":
                self.send_json(self.api_search(conn, q))
            elif path == "/api/club_opponents":
                self.send_json(self.api_club_opponents(conn, q))
            elif path == "/api/club_vs_club":
                self.send_json(self.api_club_vs_club(conn, q))
            elif path.startswith("/api/ikort/") and path.endswith("/klasman"):
                self.send_json(self.api_ikort_klasman(int(path.split("/")[-2])))
            elif path.startswith("/api/ikort/"):
                self.send_json(self.api_ikort(int(path.rsplit("/", 1)[1])))
            else:
                self.send_json({"error": "not found"}, status=404)
        finally:
            conn.close()

    # ---- endpoints ----
    def api_stats(self, conn):
        from datetime import date, timedelta
        g = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]
        cutoff = (date.today() - timedelta(days=183)).strftime("%Y%m%d")
        af = "SUBSTR(last_match_date,7,4)||SUBSTR(last_match_date,4,2)||SUBSTR(last_match_date,1,2)>=?"
        ages = rows_to_dicts(conn.execute(
            f"SELECT age_group, count(*) n FROM player_ratings WHERE age_group IS NOT NULL AND {af} GROUP BY age_group ORDER BY age_group",
            (cutoff,)
        ).fetchall())
        return {
            "players": g("SELECT count(*) FROM players"),
            "ratedPlayers": g("SELECT count(*) FROM player_ratings"),
            "activeRatedPlayers": g("SELECT count(*) FROM player_ratings"),
            "clubs": g("SELECT count(*) FROM clubs"),
            "tournaments": g("SELECT count(*) FROM tournaments"),
            "matches": g("SELECT count(*) FROM matches"),
            "completed": g("SELECT count(*) FROM matches WHERE result_type='completed'"),
            "byAgeGroup": ages,
        }

    def api_home_insights(self, conn):
        one = lambda sql, p=(): (lambda r: dict(r) if r else None)(conn.execute(sql, p).fetchone())
        many = lambda sql, p=(): rows_to_dicts(conn.execute(sql, p).fetchall())
        scal = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]
        MONTHS = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
        from datetime import date, timedelta
        acs = (date.today() - timedelta(days=183)).strftime("%Y%m%d")
        af = lambda p="": (f"SUBSTR({p}last_match_date,7,4)||SUBSTR({p}last_match_date,4,2)||SUBSTR({p}last_match_date,1,2)>='{acs}'")
        year = scal("SELECT MAX(SUBSTR(match_date,7,4)) FROM matches WHERE match_date<>''") or ""
        yf = "SUBSTR(match_date,7,4)=?"
        busiest = one(f"SELECT SUBSTR(match_date,4,2) mo,count(*) n FROM matches WHERE {yf} AND match_date<>'' GROUP BY mo ORDER BY n DESC LIMIT 1", (year,))
        top_cat = one(f"SELECT age_group,gender,count(*) n FROM matches WHERE {yf} AND age_group IS NOT NULL AND gender<>'' GROUP BY age_group,gender ORDER BY n DESC LIMIT 1", (year,))
        season = {
            "year": year,
            "matches": scal(f"SELECT count(*) FROM matches WHERE {yf}", (year,)),
            "completed": scal(f"SELECT count(*) FROM matches WHERE {yf} AND result_type='completed'", (year,)),
            "tournaments": scal("SELECT count(*) FROM tournaments WHERE year=?", (int(year) if year else 0,)),
            "busiestMonth": MONTHS[int(busiest["mo"])] if busiest else "—",
            "topCategory": f"{top_cat['age_group']} Yaş {top_cat['gender']}" if top_cat else "—",
        }
        records = {
            "peak": one("SELECT player_id,name,round(peak_rating) v FROM player_ratings ORDER BY peak_rating DESC LIMIT 1"),
            "mostMatches": one("SELECT player_id,name,matches v FROM player_ratings ORDER BY matches DESC LIMIT 1"),
            "youngest": one("SELECT player_id,name,birth_year v,round(rating) rating FROM player_ratings WHERE matches>=5 AND birth_year IS NOT NULL ORDER BY birth_year DESC,rating DESC LIMIT 1"),
            "bestPct": one("SELECT player_id,name,wins,losses,round(wins*100.0/matches) v FROM player_ratings WHERE matches>=30 ORDER BY wins*1.0/matches DESC LIMIT 1"),
            "mostTournaments": one(
                """WITH pt AS (SELECT player_id,count(DISTINCT tournament_id) v FROM (
                        SELECT winner_id player_id,tournament_id FROM matches WHERE winner_id IS NOT NULL AND tournament_id IS NOT NULL
                        UNION ALL SELECT loser_id player_id,tournament_id FROM matches WHERE loser_id IS NOT NULL AND tournament_id IS NOT NULL
                    ) GROUP BY player_id)
                    SELECT pr.player_id,pr.name,pt.v FROM player_ratings pr JOIN pt ON pt.player_id=pr.player_id ORDER BY pt.v DESC LIMIT 1"""
            ),
        }
        rise = one(
            """WITH h AS (SELECT player_id,rating_after,SUBSTR(match_date,7,4)||SUBSTR(match_date,4,2)||SUBSTR(match_date,1,2) dk
                    FROM player_rating_history),
                strt AS (SELECT player_id,count(*) c,MIN(dk) mindk FROM h GROUP BY player_id)
                SELECT pr.player_id,pr.name,CAST(round(pr.rating-(SELECT rating_after FROM h WHERE h.player_id=strt.player_id AND h.dk=strt.mindk LIMIT 1)) AS INTEGER) v
                FROM strt JOIN player_ratings pr ON pr.player_id=strt.player_id WHERE strt.c>=5 ORDER BY v DESC LIMIT 1"""
        )
        if rise and rise.get("v") is not None:
            rise["v"] = (f"+{rise['v']}" if rise["v"] > 0 else str(rise["v"]))
        records["biggestRise"] = rise
        top_clubs = many(f"SELECT club_id,club_name,sum(wins) w,sum(losses) l,round(sum(wins)*100.0/(sum(wins)+sum(losses))) pct,count(*) n FROM player_ratings WHERE club_id IS NOT NULL AND club_name<>'' AND matches>=5 AND {af()} GROUP BY club_id HAVING n>=20 AND (w+l)>0 ORDER BY pct DESC LIMIT 8")
        max_birth = scal(f"SELECT MAX(birth_year) FROM player_ratings WHERE matches>=5 AND {af()}")
        young_talents = many(f"SELECT player_id,name,birth_year,rating,club_name FROM player_ratings WHERE birth_year>=? AND matches>=5 AND {af()} ORDER BY rating DESC LIMIT 10", (max_birth - 1,))
        upsets = many(
            f"""SELECT m.winner_id,wr.name wn,round(wr.rating) wrating,m.loser_id,lr.name ln,round(lr.rating) lrating,round(lr.rating-wr.rating) gap,m.match_date,m.event
               FROM matches m JOIN player_ratings wr ON wr.player_id=m.winner_id JOIN player_ratings lr ON lr.player_id=m.loser_id
               WHERE m.result_type='completed' AND wr.matches>=10 AND lr.matches>=10 AND {af('wr.')} AND {af('lr.')} AND lr.rating-wr.rating>0
               ORDER BY gap DESC LIMIT 6"""
        )
        cohorts = many(f"SELECT birth_year,round(avg(rating)) avg,count(*) n FROM player_ratings WHERE birth_year IS NOT NULL AND matches>=5 AND {af()} GROUP BY birth_year HAVING n>=20 ORDER BY birth_year DESC LIMIT 8")
        cities = many("SELECT p.city,count(*) n FROM player_ratings pr JOIN players p ON p.player_id=pr.player_id WHERE p.city<>'' AND p.city IS NOT NULL GROUP BY p.city ORDER BY n DESC LIMIT 10")
        return {"season": season, "records": records, "topClubs": top_clubs, "youngTalents": young_talents, "upsets": upsets, "cohorts": cohorts, "cities": cities}

    def api_rankings(self, conn, q):
        where, params = ["1=1"], []
        if q.get("age_group"):
            where.append("pr.age_group=?"); params.append(int(q["age_group"]))
        if q.get("club_id"):
            where.append("p.club_id=?"); params.append(int(q["club_id"]))
        if q.get("gender"):
            where.append("p.gender=?"); params.append(q["gender"])
        if q.get("birth_year"):
            where.append("p.birth_year=?"); params.append(int(q["birth_year"]))
        if q.get("city"):
            where.append("p.city=?"); params.append(q["city"])
        if q.get("q"):
            where.append(f"{foldsql('p.name')} LIKE ?"); params.append(f"%{fold(q['q'])}%")
        if q.get("active_only"):
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=183)).strftime("%Y%m%d")
            where.append(f"SUBSTR(pr.last_match_date,7,4)||SUBSTR(pr.last_match_date,4,2)||SUBSTR(pr.last_match_date,1,2)>='{cutoff}'")
        if q.get("min_matches") is not None:
            where.append("pr.matches>=?"); params.append(int(q["min_matches"]))
        limit = min(int(q.get("limit", 50)), 500)
        offset = int(q.get("offset", 0))
        if q.get("age_group"):
            order_col = "pr.age_group_rank"
        elif q.get("gender"):
            order_col = "pr.gender_rank"
        else:
            order_col = "pr.overall_rank"
        ag = int(q["age_group"]) if q.get("age_group") else None
        ag_sel = (
            f",(SELECT count(*) FROM matches WHERE winner_id=p.player_id AND age_group={ag} AND result_type='completed') ag_wins"
            f",(SELECT count(*) FROM matches WHERE loser_id=p.player_id AND age_group={ag} AND result_type='completed') ag_losses"
        ) if ag else ""
        sql = f"""
            SELECT p.player_id, p.name, p.birth_year, p.gender, p.club_id, p.club_name, p.city AS player_city, cl.abbrev AS club_abbrev,
                   pr.rating, pr.peak_rating, pr.matches, pr.wins, pr.losses, pr.age_group,
                   pr.first_match_date, pr.last_match_date, pr.overall_rank, pr.gender_rank, pr.age_group_rank,
                   (SELECT puan FROM klasman_puan WHERE player_id=p.player_id ORDER BY year DESC,week DESC,type DESC LIMIT 1) kp{ag_sel}
            FROM players p LEFT JOIN player_ratings pr ON pr.player_id=p.player_id
            LEFT JOIN clubs cl ON cl.club_id=p.club_id
            WHERE {' AND '.join(where)} ORDER BY ({order_col} IS NULL),{order_col},p.name LIMIT ? OFFSET ?
        """
        rows = rows_to_dicts(conn.execute(sql, (*params, limit, offset)).fetchall())
        if ag:
            for r in rows:
                r["wins"] = r.pop("ag_wins", r["wins"])
                r["losses"] = r.pop("ag_losses", r["losses"])
        total = conn.execute(
            f"SELECT count(*) FROM players p LEFT JOIN player_ratings pr ON pr.player_id=p.player_id WHERE {' AND '.join(where)}",
            params,
        ).fetchone()[0]
        years = [r[0] for r in conn.execute("SELECT DISTINCT birth_year FROM players WHERE birth_year IS NOT NULL ORDER BY birth_year").fetchall()]
        return {"total": total, "limit": limit, "offset": offset, "years": years, "rankings": rows}

    def api_player(self, conn, pid):
        rating = conn.execute("SELECT * FROM player_ratings WHERE player_id=?", (pid,)).fetchone()
        base = conn.execute("SELECT * FROM players WHERE player_id=?", (pid,)).fetchone()
        if not rating and not base:
            return {"error": "player not found"}
        matches = player_matches(conn, pid)
        # record by stage
        stages = {}
        opp = {}
        for m in matches:
            st = m["stage"] or "—"
            d = stages.setdefault(st, {"w": 0, "l": 0})
            d["w" if m["won"] else "l"] += 1
            o = opp.setdefault(m["opponentId"], {"name": m["opponentName"], "w": 0, "l": 0})
            o["w" if m["won"] else "l"] += 1
        top_opponents = sorted(
            [{"playerId": k, **v, "total": v["w"] + v["l"]} for k, v in opp.items()],
            key=lambda x: x["total"], reverse=True,
        )[:8]
        kh = conn.execute(
            "SELECT year, week, type, puan FROM klasman_puan WHERE player_id=? ORDER BY year, week",
            (pid,),
        ).fetchall()
        return {
            "player": dict(base) if base else None,
            "rating": dict(rating) if rating else None,
            "recentMatches": matches[:25],
            "totalMatches": len(matches),
            "stageRecord": stages,
            "topOpponents": top_opponents,
            "klasmanHistory": [dict(r) for r in kh],
        }

    def api_h2h(self, conn, a, b):
        rows = conn.execute(
            """SELECT m.match_id, m.match_date, m.event, m.stage, m.age_group, m.winner_id, m.loser_id, m.p1_id,
                      m.tournament_id, t.name AS tname
               FROM matches m LEFT JOIN tournaments t ON t.tournament_id=m.tournament_id
               WHERE m.result_type='completed'
               AND ((m.winner_id=? AND m.loser_id=?) OR (m.winner_id=? AND m.loser_id=?))""",
            (a, b, b, a),
        ).fetchall()
        na = conn.execute("SELECT name FROM players WHERE player_id=?", (a,)).fetchone()
        nb = conn.execute("SELECT name FROM players WHERE player_id=?", (b,)).fetchone()
        wins_a = sum(1 for r in rows if r["winner_id"] == a)
        out = []
        for r in rows:
            out.append({
                "date": r["match_date"], "event": r["event"], "stage": r["stage"], "ageGroup": r["age_group"],
                "winnerId": r["winner_id"], "aWon": r["winner_id"] == a,
                "tournamentId": r["tournament_id"], "tournamentName": r["tname"],
                "score": build_score(conn, r["match_id"], player_is_first=(r["p1_id"] == a)),
                "sets": build_set_cols(conn, r["match_id"], player_is_first=(r["p1_id"] == r["winner_id"])),
            })
        return {
            "a": {"playerId": a, "name": na["name"] if na else f"#{a}", "wins": wins_a},
            "b": {"playerId": b, "name": nb["name"] if nb else f"#{b}", "wins": len(rows) - wins_a},
            "total": len(rows), "matches": out,
        }

    def _opponent_record(self, conn, pid):
        rows = conn.execute(
            """SELECT winner_id, loser_id FROM matches
               WHERE result_type='completed' AND winner_id IS NOT NULL AND loser_id IS NOT NULL
               AND (winner_id=? OR loser_id=?)""",
            (pid, pid),
        ).fetchall()
        rec = {}
        for r in rows:
            won = r["winner_id"] == pid
            opp = r["loser_id"] if won else r["winner_id"]
            d = rec.setdefault(opp, {"w": 0, "l": 0})
            d["w" if won else "l"] += 1
        return rec

    def _matches_between(self, conn, p, opp):
        rows = conn.execute(
            """SELECT m.match_id, m.match_date, m.event, m.stage, m.winner_id, m.p1_id,
                      m.tournament_id, t.name AS tname
               FROM matches m LEFT JOIN tournaments t ON t.tournament_id=m.tournament_id
               WHERE m.result_type='completed'
               AND ((m.winner_id=? AND m.loser_id=?) OR (m.winner_id=? AND m.loser_id=?))""",
            (p, opp, opp, p),
        ).fetchall()
        out = []
        for r in rows:
            won = r["winner_id"] == p
            out.append({
                "date": r["match_date"], "event": r["event"], "stage": r["stage"],
                "won": won, "tournamentId": r["tournament_id"], "tournamentName": r["tname"],
                "score": build_score(conn, r["match_id"], player_is_first=(r["p1_id"] == p)),
            })
        return out

    def api_common(self, conn, a, b):
        ra, rb = self._opponent_record(conn, a), self._opponent_record(conn, b)
        common_ids = (set(ra) & set(rb)) - {a, b}
        na = conn.execute("SELECT name FROM players WHERE player_id=?", (a,)).fetchone()
        nb = conn.execute("SELECT name FROM players WHERE player_id=?", (b,)).fetchone()
        names = {}
        for oid in common_ids:
            row = conn.execute("SELECT name FROM players WHERE player_id=?", (oid,)).fetchone()
            names[oid] = row["name"] if row else f"#{oid}"
        common = []
        a_sum = {"w": 0, "l": 0}
        b_sum = {"w": 0, "l": 0}
        a_better = b_better = even = 0
        for oid in common_ids:
            aw, al = ra[oid]["w"], ra[oid]["l"]
            bw, bl = rb[oid]["w"], rb[oid]["l"]
            a_sum["w"] += aw; a_sum["l"] += al
            b_sum["w"] += bw; b_sum["l"] += bl
            ar = aw / (aw + al) if aw + al else 0
            br = bw / (bw + bl) if bw + bl else 0
            if ar > br: a_better += 1
            elif br > ar: b_better += 1
            else: even += 1
            common.append({
                "opponentId": oid, "name": names[oid],
                "aW": aw, "aL": al, "bW": bw, "bL": bl,
                "total": aw + al + bw + bl,
                "aMatches": self._matches_between(conn, a, oid),
                "bMatches": self._matches_between(conn, b, oid),
            })
        common.sort(key=lambda x: x["total"], reverse=True)
        return {
            "a": {"playerId": a, "name": na["name"] if na else f"#{a}"},
            "b": {"playerId": b, "name": nb["name"] if nb else f"#{b}"},
            "commonCount": len(common_ids),
            "aVsCommon": a_sum, "bVsCommon": b_sum,
            "aBetter": a_better, "bBetter": b_better, "even": even,
            "common": common,
        }

    def api_players(self, conn, q):
        where, params = ["1=1"], []
        if q.get("q"):
            where.append(f"{foldsql('p.name')} LIKE ?"); params.append(f"%{fold(q['q'])}%")
        if q.get("gender"):
            where.append("p.gender=?"); params.append(q["gender"])
        if q.get("birth_year"):
            where.append("p.birth_year=?"); params.append(int(q["birth_year"]))
        if q.get("city"):
            where.append("p.city=?"); params.append(q["city"])
        if q.get("club_id"):
            where.append("p.club_id=?"); params.append(int(q["club_id"]))
        if q.get("rated") == "1":
            where.append("pr.player_id IS NOT NULL")
        limit = min(int(q.get("limit", 50)), 200)
        offset = int(q.get("offset", 0))
        wc = " AND ".join(where)
        sql = f"""
            SELECT p.player_id, p.name, p.birth_year, p.gender, p.club_id, p.club_name, p.city,
                   pr.rating, pr.matches, pr.age_group
            FROM players p LEFT JOIN player_ratings pr ON pr.player_id=p.player_id
            WHERE {wc}
            ORDER BY (pr.rating IS NULL), pr.rating DESC, p.name
            LIMIT ? OFFSET ?
        """
        rows = rows_to_dicts(conn.execute(sql, (*params, limit, offset)).fetchall())
        total = conn.execute(
            f"SELECT count(*) FROM players p LEFT JOIN player_ratings pr ON pr.player_id=p.player_id WHERE {wc}",
            params,
        ).fetchone()[0]
        years = [r[0] for r in conn.execute("SELECT DISTINCT birth_year FROM players WHERE birth_year IS NOT NULL ORDER BY birth_year").fetchall()]
        return {"total": total, "limit": limit, "offset": offset, "years": years, "players": rows}

    def api_tournaments(self, conn, q):
        where, params = ["1=1"], []
        if q.get("include_ongoing"):
            where.append("t.source_tab IN ('gecmis','guncel')")
        else:
            where.append("t.source_tab='gecmis'")
        if q.get("q"):
            where.append(f"({foldsql('t.name')} LIKE ? OR {foldsql('t.title')} LIKE ?)"); params += [f"%{fold(q['q'])}%", f"%{fold(q['q'])}%"]
        if q.get("city"):
            where.append("t.city=?"); params.append(q["city"])
        if q.get("year"):
            where.append("t.year=?"); params.append(int(q["year"]))
        if q.get("age_group"):
            where.append("EXISTS(SELECT 1 FROM matches mx WHERE mx.tournament_id=t.tournament_id AND mx.age_group=?)"); params.append(int(q["age_group"]))
        limit = min(int(q.get("limit", 700)), 700)
        mc_sql = "(SELECT count(*) FROM matches m WHERE m.tournament_id=t.tournament_id AND m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL)"
        pc_sql = "(SELECT count(*) FROM (SELECT winner_id v FROM matches WHERE tournament_id=t.tournament_id AND winner_id IS NOT NULL UNION SELECT loser_id FROM matches WHERE tournament_id=t.tournament_id AND loser_id IS NOT NULL))"
        dord = "SUBSTR(REPLACE(t.start_date,' ',''),7,4)||SUBSTR(REPLACE(t.start_date,' ',''),4,2)||SUBSTR(REPLACE(t.start_date,' ',''),1,2)"
        order = f"{mc_sql} DESC, {dord} DESC, t.tournament_id DESC" if q.get("sort") == "size" else f"{dord} DESC, t.tournament_id DESC"
        sql = f"""
            SELECT t.tournament_id, t.name, t.title, t.city, t.start_date, t.year, t.type_text, t.surface, t.court_type,
                   t.source_tab, c.name AS club_name, {mc_sql} AS match_count, {pc_sql} AS player_count
            FROM tournaments t LEFT JOIN clubs c ON c.club_id=t.club_id
            WHERE {' AND '.join(where)} AND ({mc_sql}>0 OR t.source_tab='guncel')
            ORDER BY {order} LIMIT ?
        """
        rows = rows_to_dicts(conn.execute(sql, (*params, limit)).fetchall())
        years = [r[0] for r in conn.execute("SELECT DISTINCT year FROM tournaments WHERE year IS NOT NULL ORDER BY year DESC").fetchall()]
        age_groups = [r[0] for r in conn.execute("SELECT DISTINCT age_group FROM matches WHERE age_group IS NOT NULL ORDER BY age_group").fetchall()]
        cities = [r[0] for r in conn.execute("SELECT DISTINCT city FROM tournaments WHERE city IS NOT NULL AND city<>'' ORDER BY city").fetchall()]
        return {"total": len(rows), "years": years, "age_groups": age_groups, "cities": cities, "tournaments": rows}

    def api_tournament(self, conn, tid):
        t = conn.execute("SELECT * FROM tournaments WHERE tournament_id=?", (tid,)).fetchone()
        if not t:
            return {"error": "tournament not found"}
        club = conn.execute("SELECT name, city FROM clubs WHERE club_id=?", (t["club_id"],)).fetchone() if t["club_id"] else None
        cats = rows_to_dicts(conn.execute(
            """SELECT event, age_group, gender, count(*) n,
                      sum(CASE WHEN result_type='completed' THEN 1 ELSE 0 END) completed
               FROM matches WHERE tournament_id=? AND event<>'' GROUP BY event ORDER BY n DESC""",
            (tid,),
        ).fetchall())
        # champion per event = winner of a Final-stage match; finalist = its loser
        champs, finals = {}, {}
        for r in conn.execute(
            "SELECT event, winner_id, loser_id FROM matches WHERE tournament_id=? AND stage LIKE '%Final%' AND winner_id IS NOT NULL",
            (tid,),
        ).fetchall():
            if r["event"] not in champs:
                champs[r["event"]] = r["winner_id"]
                finals[r["event"]] = r["loser_id"]
        # matches
        mrows = conn.execute(
            """SELECT match_id, event, stage, gender, day_name, match_date, court, start_time, match_code, result_type,
                      winner_id, loser_id, p1_id FROM matches WHERE tournament_id=?""",
            (tid,),
        ).fetchall()
        ids = set()
        for r in mrows:
            ids.update([r["winner_id"], r["loser_id"]])
        ids |= set(champs.values()) | set(finals.values())
        names = {}
        for pid in ids:
            if pid is None:
                continue
            row = conn.execute("SELECT name FROM players WHERE player_id=?", (pid,)).fetchone()
            names[pid] = row["name"] if row else f"#{pid}"

        def rating_at(pid, dk):
            if pid is None or not dk:
                return None
            row = conn.execute(
                "SELECT rating_after FROM player_rating_history WHERE player_id=? "
                "AND SUBSTR(match_date,7,4)||SUBSTR(match_date,4,2)||SUBSTR(match_date,1,2)<? "
                "ORDER BY SUBSTR(match_date,7,4)||SUBSTR(match_date,4,2)||SUBSTR(match_date,1,2) DESC, rowid DESC LIMIT 1",
                (pid, dk),
            ).fetchone()
            return round(row["rating_after"]) if row else None

        matches = []
        for r in mrows:
            if r["winner_id"] is None or r["loser_id"] is None:
                continue
            d = r["match_date"] or ""
            dk = (d[6:10] + d[3:5] + d[0:2]) if len(d) >= 10 else ""
            matches.append({
                "event": r["event"], "stage": r["stage"], "gender": r["gender"], "dayName": r["day_name"],
                "date": r["match_date"], "court": r["court"], "startTime": r["start_time"], "matchCode": r["match_code"],
                "resultType": r["result_type"],
                "winnerId": r["winner_id"], "winnerName": names.get(r["winner_id"]),
                "loserId": r["loser_id"], "loserName": names.get(r["loser_id"]),
                "winnerRating": rating_at(r["winner_id"], dk), "loserRating": rating_at(r["loser_id"], dk),
                "score": build_score(conn, r["match_id"], player_is_first=(r["p1_id"] == r["winner_id"])),
                "sets": build_set_cols(conn, r["match_id"], player_is_first=(r["p1_id"] == r["winner_id"])),
            })
        for c in cats:
            cid = champs.get(c["event"])
            fid = finals.get(c["event"])
            c["championId"] = cid
            c["championName"] = names.get(cid) if cid else None
            c["finalistId"] = fid
            c["finalistName"] = names.get(fid) if fid else None
        return {"tournament": dict(t), "club": dict(club) if club else None,
                "categories": cats, "matches": matches}

    def api_clubs(self, conn, q):
        where, params = ["1=1"], []
        if q.get("q"):
            where.append(f"{foldsql('c.name')} LIKE ?"); params.append(f"%{fold(q['q'])}%")
        if q.get("city"):
            where.append("c.city=?"); params.append(q["city"])
        limit = min(int(q.get("limit", 100)), 600)
        order_map = {
            "elo": "pr.avg_rating IS NULL, pr.avg_rating DESC",
            "pct": "((coalesce(total_wins,0)+coalesce(total_losses,0))>=20) DESC, coalesce(total_wins,0)*1.0/nullif(coalesce(total_wins,0)+coalesce(total_losses,0),0) DESC",
            "rated": "rated_count DESC",
            "players": "player_count DESC",
        }
        order = order_map.get(q.get("sort"), order_map["players"])
        sql = f"""
            SELECT c.club_id, c.name, c.abbrev, c.city,
                   coalesce(pc.n, 0) AS player_count,
                   coalesce(pr.rated_count, 0) AS rated_count,
                   coalesce(pr.total_wins, 0) AS total_wins,
                   coalesce(pr.total_losses, 0) AS total_losses,
                   pr.avg_rating
            FROM clubs c
            LEFT JOIN (SELECT club_id, count(*) n FROM players GROUP BY club_id) pc ON pc.club_id=c.club_id
            LEFT JOIN (SELECT club_id, count(*) rated_count, sum(wins) total_wins, sum(losses) total_losses, round(avg(rating)) avg_rating FROM player_ratings GROUP BY club_id) pr ON pr.club_id=c.club_id
            WHERE {' AND '.join(where)}
            ORDER BY {order} LIMIT ?
        """
        cities = [r[0] for r in conn.execute("SELECT DISTINCT city FROM clubs WHERE city IS NOT NULL AND city<>'' ORDER BY city").fetchall()]
        return {"clubs": rows_to_dicts(conn.execute(sql, (*params, limit)).fetchall()), "cities": cities}

    def api_club(self, conn, club_id):
        c = conn.execute("""
            SELECT c.club_id, c.name, c.abbrev, c.city,
                   coalesce(pc.n, 0) AS player_count,
                   coalesce(pr.rated_count, 0) AS rated_count,
                   coalesce(pr.total_wins, 0) AS total_wins,
                   coalesce(pr.total_losses, 0) AS total_losses,
                   pr.avg_rating
            FROM clubs c
            LEFT JOIN (SELECT club_id, count(*) n FROM players GROUP BY club_id) pc ON pc.club_id=c.club_id
            LEFT JOIN (SELECT club_id, count(*) rated_count, sum(wins) total_wins, sum(losses) total_losses, round(avg(rating)) avg_rating FROM player_ratings GROUP BY club_id) pr ON pr.club_id=c.club_id
            WHERE c.club_id=?""", (club_id,)).fetchone()
        if not c:
            return {"error": "not found"}
        c = dict(c)
        c["tournament_count"] = conn.execute("SELECT count(*) FROM tournaments WHERE club_id=?", (club_id,)).fetchone()[0]
        from datetime import date, timedelta
        cutoff = (date.today() - timedelta(days=183)).strftime("%Y%m%d")
        players = rows_to_dicts(conn.execute("""
            SELECT p.player_id, p.name, p.birth_year, p.gender,
                   pr.rating, pr.age_group, pr.overall_rank, pr.age_group_rank, pr.gender_rank,
                   pr.wins, pr.losses, pr.matches
            FROM players p JOIN player_ratings pr ON pr.player_id=p.player_id
            WHERE p.club_id=?
            AND pr.last_match_date IS NOT NULL
            AND SUBSTR(pr.last_match_date,7,4)||SUBSTR(pr.last_match_date,4,2)||SUBSTR(pr.last_match_date,1,2)>=?
            ORDER BY pr.rating DESC""", (club_id, cutoff)).fetchall())
        tournaments = rows_to_dicts(conn.execute("""
            SELECT tournament_id, name, city, start_date, year, type_text
            FROM tournaments WHERE club_id=? ORDER BY year DESC, tournament_id DESC LIMIT 20""", (club_id,)).fetchall())
        return {"club": dict(c), "players": players, "tournaments": tournaments}

    def api_ikort(self, pid):
        import urllib.request, re as _re, ssl
        url = f"https://ikort.com.tr/oyuncu-profil/{pid}?page=genelbilgi"
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "tr"})
            with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            return {"error": str(e)}
        def extract(label):
            # Skip HTML tags (attrs may contain numbers) to reach the visible text value
            m = _re.search(label + r'(?:[^<\d]|<[^>]*>)*?(\d+)', html, _re.I)
            return int(m.group(1)) if m else None
        return {
            "playerId": pid,
            "ikortUrl": url,
            "klasmanPuan": extract(r"Genel Klasman Puan"),
            "klasmanSira": extract(r"Genel Klasman S[ıi]ra"),
        }

    def api_ikort_klasman(self, pid):
        import urllib.request, re as _re, ssl
        url = f"https://ikort.com.tr/oyuncu-profil/{pid}?page=genelklasman"
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "tr"})
            with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception as e:
            return {"error": str(e)}
        def xnum(label):
            m = _re.search(label + r'(?:[^<\d]|<[^>]*>)*?(\d+)', html, _re.I)
            return int(m.group(1)) if m else None
        def clean(s):
            return ' '.join(_re.sub(r'<[^>]+>', ' ', s).split())
        summary = {
            "puan": xnum(r"Genel Klasman Puan"),
            "sira": xnum(r"Genel Klasman S[ıi]ra"),
            "ulusal": xnum(r"Ulusal Puan"),
            "uluslararasi": xnum(r"Uluslararas[ıi] Puan"),
        }
        # Collect section headings (h2-h5) and data rows by position, then sort
        items = []
        for m in _re.finditer(r'<h[2-5]\b[^>]*>(.*?)</h[2-5]>', html, _re.S | _re.I):
            t = clean(m.group(1))
            if t and len(t) < 120:
                items.append((m.start(), "header", [t]))
        for m in _re.finditer(r'<tr\b([^>]*)>(.*?)</tr>', html, _re.S | _re.I):
            tr_attrs, tr_html = m.group(1), m.group(2)
            ths = [clean(th) for th in _re.findall(r'<th\b[^>]*>(.*?)</th>', tr_html, _re.S | _re.I)]
            raw_tds = _re.findall(r'<td\b[^>]*>(.*?)</td>', tr_html, _re.S | _re.I)
            tds = [clean(td) for td in raw_tds]
            if ths and not tds:
                pass  # skip column-header rows
            elif tds and any(_re.search(r'\d{2}[-./]\d{2}[-./]\d{4}', c) for c in tds):
                # counted: check tr class or first td raw content
                tr_cls = (_re.search(r'class=["\']([^"\']+)', tr_attrs) or _re.search(r'$', '')).group(1) if _re.search(r'class=', tr_attrs) else ''
                sayilan_raw = raw_tds[0] if raw_tds else ''
                counted = bool(_re.search(r'pointsticked', sayilan_raw, _re.I))
                items.append((m.start(), "data", tds, sayilan_raw[:800], counted))
        items.sort(key=lambda x: x[0])
        rows = []
        for item in items:
            if item[1] == "header":
                rows.append({"type": "header", "cells": item[2]})
            else:
                rows.append({"type": "data", "cells": item[2], "sayilan": item[3], "counted": item[4]})
        return {"summary": summary, "rows": rows}

    def api_club_opponents(self, conn, q):
        pid = int(q.get("player", 0) or 0)
        cid = int(q.get("club_id", 0) or 0)
        if not pid or not cid:
            return {"opponents": []}
        rows = conn.execute(
            """SELECT m.match_id, m.match_date, m.event, m.stage, m.winner_id, m.loser_id, m.p1_id,
                      m.tournament_id, t.name AS tournament_name,
                      opp.player_id AS opp_id, opp.name AS opp_name
               FROM matches m
               LEFT JOIN tournaments t ON t.tournament_id=m.tournament_id
               JOIN players opp ON opp.player_id=CASE WHEN m.winner_id=? THEN m.loser_id ELSE m.winner_id END
               WHERE (m.winner_id=? OR m.loser_id=?) AND m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL
                 AND opp.club_id=?
               ORDER BY SUBSTR(REPLACE(m.match_date,' ',''),7,4)||SUBSTR(REPLACE(m.match_date,' ',''),4,2)||SUBSTR(REPLACE(m.match_date,' ',''),1,2) DESC""",
            (pid, pid, pid, cid),
        ).fetchall()
        by_opp: dict = {}
        for r in rows:
            oid = r["opp_id"]
            if oid not in by_opp:
                by_opp[oid] = {"oppId": oid, "oppName": r["opp_name"], "w": 0, "l": 0, "matches": []}
            won = r["winner_id"] == pid
            by_opp[oid]["w" if won else "l"] += 1
            by_opp[oid]["matches"].append({
                "date": r["match_date"], "event": r["event"], "stage": r["stage"], "won": won,
                "score": build_score(conn, r["match_id"], r["p1_id"] == pid),
                "tournamentId": r["tournament_id"], "tournamentName": r["tournament_name"],
            })
        opponents = sorted(by_opp.values(), key=lambda x: x["w"] + x["l"], reverse=True)
        return {"opponents": opponents}

    def api_club_vs_club(self, conn, q):
        ca = int(q.get("club_a", 0) or 0)
        cb = int(q.get("club_b", 0) or 0)
        lim = min(int(q.get("limit", 150) or 150), 400)
        if not ca or not cb:
            return {"matches": [], "aWins": 0, "bWins": 0, "total": 0, "byGender": {}}
        rows = rows_to_dicts(conn.execute(
            """SELECT m.match_id, m.match_date, m.event, m.stage, m.winner_id, m.loser_id,
                      m.p1_id, m.tournament_id, t.name AS tournament_name,
                      pw.name AS winner_name, pw.club_id AS winner_club,
                      pl.name AS loser_name, pl.club_id AS loser_club,
                      pr.gender AS match_gender
               FROM matches m
               LEFT JOIN tournaments t ON t.tournament_id=m.tournament_id
               JOIN players pw ON pw.player_id=m.winner_id
               JOIN players pl ON pl.player_id=m.loser_id
               LEFT JOIN player_ratings pr ON pr.player_id=m.winner_id
               WHERE m.winner_id IS NOT NULL AND m.loser_id IS NOT NULL
                 AND ((pw.club_id=? AND pl.club_id=?) OR (pw.club_id=? AND pl.club_id=?))
               ORDER BY SUBSTR(REPLACE(m.match_date,' ',''),7,4)||SUBSTR(REPLACE(m.match_date,' ',''),4,2)||SUBSTR(REPLACE(m.match_date,' ',''),1,2) DESC
               LIMIT ?""",
            (ca, cb, cb, ca, lim),
        ).fetchall())
        a_wins = 0
        b_wins = 0
        by_gender: dict = {}
        for r in rows:
            aw = r["winner_club"] == ca
            if aw:
                a_wins += 1
            else:
                b_wins += 1
            g = r.get("match_gender") or "Diğer"
            if g not in by_gender:
                by_gender[g] = {"aWins": 0, "bWins": 0, "total": 0}
            by_gender[g]["total"] += 1
            if aw:
                by_gender[g]["aWins"] += 1
            else:
                by_gender[g]["bWins"] += 1
        for r in rows:
            first_is_winner = r["winner_id"] == r["p1_id"]
            r["score"] = build_set_score(conn, r["match_id"], first_is_winner)
            r["fullScore"] = build_score(conn, r["match_id"], player_is_first=first_is_winner)
            r["sets"] = build_set_cols(conn, r["match_id"], player_is_first=first_is_winner)
        return {"matches": rows, "aWins": a_wins, "bWins": b_wins, "total": len(rows), "byGender": by_gender}

    def api_search(self, conn, q):
        term = q.get("q", "").strip()
        if len(term) < 2:
            return {"players": [], "clubs": []}
        like = f"%{fold(term)}%"
        players = rows_to_dicts(conn.execute(
            f"""SELECT pr.player_id, pr.name, pr.age_group, pr.rating, pr.club_name
               FROM player_ratings pr WHERE {foldsql('pr.name')} LIKE ? ORDER BY pr.rating DESC LIMIT 15""",
            (like,),
        ).fetchall())
        clubs = rows_to_dicts(conn.execute(
            f"SELECT club_id, name, city FROM clubs WHERE {foldsql('name')} LIKE ? LIMIT 10", (like,)
        ).fetchall())
        return {"players": players, "clubs": clubs}


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"DB yok: {DB_PATH} — önce build_db.py + build_ratings.py çalıştır")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"tennos http://localhost:{PORT}  (Ctrl+C ile dur)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
