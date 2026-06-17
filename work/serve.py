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
            elif path == "/api/search":
                self.send_json(self.api_search(conn, q))
            elif path == "/api/club_opponents":
                self.send_json(self.api_club_opponents(conn, q))
            else:
                self.send_json({"error": "not found"}, status=404)
        finally:
            conn.close()

    # ---- endpoints ----
    def api_stats(self, conn):
        g = lambda sql: conn.execute(sql).fetchone()[0]
        ages = rows_to_dicts(conn.execute(
            "SELECT age_group, count(*) n FROM player_ratings WHERE age_group IS NOT NULL GROUP BY age_group ORDER BY age_group"
        ).fetchall())
        return {
            "players": g("SELECT count(*) FROM players"),
            "ratedPlayers": g("SELECT count(*) FROM player_ratings"),
            "clubs": g("SELECT count(*) FROM clubs"),
            "tournaments": g("SELECT count(*) FROM tournaments"),
            "matches": g("SELECT count(*) FROM matches"),
            "completed": g("SELECT count(*) FROM matches WHERE result_type='completed'"),
            "byAgeGroup": ages,
        }

    def api_rankings(self, conn, q):
        where, params = ["1=1"], []
        if q.get("age_group"):
            where.append("pr.age_group=?"); params.append(int(q["age_group"]))
        if q.get("club_id"):
            where.append("pr.club_id=?"); params.append(int(q["club_id"]))
        if q.get("gender"):
            where.append("pr.gender=?"); params.append(q["gender"])
        if q.get("birth_year"):
            where.append("pr.birth_year=?"); params.append(int(q["birth_year"]))
        if q.get("q"):
            where.append(f"{foldsql('pr.name')} LIKE ?"); params.append(f"%{fold(q['q'])}%")
        if not q.get("all_time"):
            from datetime import date, timedelta
            cutoff = (date.today() - timedelta(days=365)).strftime("%Y%m%d")
            where.append(f"SUBSTR(pr.last_match_date,7,4)||SUBSTR(pr.last_match_date,4,2)||SUBSTR(pr.last_match_date,1,2)>='{cutoff}'")
        min_matches = int(q.get("min_matches", 5))
        where.append("pr.matches>=?"); params.append(min_matches)
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
            f",(SELECT count(*) FROM matches WHERE winner_id=pr.player_id AND age_group={ag} AND result_type='completed') ag_wins"
            f",(SELECT count(*) FROM matches WHERE loser_id=pr.player_id AND age_group={ag} AND result_type='completed') ag_losses"
        ) if ag else ""
        sql = f"""
            SELECT pr.*, p.gender, p.city AS player_city{ag_sel}
            FROM player_ratings pr LEFT JOIN players p ON p.player_id=pr.player_id
            WHERE {' AND '.join(where)} ORDER BY {order_col} LIMIT ? OFFSET ?
        """
        rows = rows_to_dicts(conn.execute(sql, (*params, limit, offset)).fetchall())
        if ag:
            for r in rows:
                r["wins"] = r.pop("ag_wins", r["wins"])
                r["losses"] = r.pop("ag_losses", r["losses"])
        total = conn.execute(
            f"SELECT count(*) FROM player_ratings pr LEFT JOIN players p ON p.player_id=pr.player_id WHERE {' AND '.join(where)}",
            params,
        ).fetchone()[0]
        years = [r[0] for r in conn.execute("SELECT DISTINCT birth_year FROM player_ratings WHERE birth_year IS NOT NULL ORDER BY birth_year").fetchall()]
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
        return {
            "player": dict(base) if base else None,
            "rating": dict(rating) if rating else None,
            "recentMatches": matches[:25],
            "totalMatches": len(matches),
            "stageRecord": stages,
            "topOpponents": top_opponents,
        }

    def api_h2h(self, conn, a, b):
        rows = conn.execute(
            """SELECT m.match_id, m.match_date, m.event, m.stage, m.winner_id, m.loser_id, m.p1_id,
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
                "date": r["match_date"], "event": r["event"], "stage": r["stage"],
                "winnerId": r["winner_id"], "aWon": r["winner_id"] == a,
                "tournamentId": r["tournament_id"], "tournamentName": r["tname"],
                "score": build_score(conn, r["match_id"], player_is_first=(r["p1_id"] == a)),
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
        sql = f"""
            SELECT t.tournament_id, t.name, t.city, t.start_date, t.year, t.type_text, t.surface, t.court_type,
                   c.name AS club_name, {mc_sql} AS match_count
            FROM tournaments t LEFT JOIN clubs c ON c.club_id=t.club_id
            WHERE {' AND '.join(where)} AND ({mc_sql}>0 OR t.source_tab='guncel')
            ORDER BY SUBSTR(REPLACE(t.start_date,' ',''),7,4)||SUBSTR(REPLACE(t.start_date,' ',''),4,2)||SUBSTR(REPLACE(t.start_date,' ',''),1,2) DESC,
                t.tournament_id DESC LIMIT ?
        """
        rows = rows_to_dicts(conn.execute(sql, (*params, limit)).fetchall())
        years = [r[0] for r in conn.execute("SELECT DISTINCT year FROM tournaments WHERE year IS NOT NULL ORDER BY year DESC").fetchall()]
        age_groups = [r[0] for r in conn.execute("SELECT DISTINCT age_group FROM matches WHERE age_group IS NOT NULL ORDER BY age_group").fetchall()]
        return {"total": len(rows), "years": years, "age_groups": age_groups, "tournaments": rows}

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
        # champion per event = winner of a Final-stage match
        champs = {}
        for r in conn.execute(
            "SELECT event, winner_id FROM matches WHERE tournament_id=? AND stage LIKE '%Final%' AND winner_id IS NOT NULL",
            (tid,),
        ).fetchall():
            champs.setdefault(r["event"], r["winner_id"])
        # matches
        mrows = conn.execute(
            """SELECT match_id, event, stage, day_name, match_date, court, result_type,
                      winner_id, loser_id, p1_id FROM matches WHERE tournament_id=?""",
            (tid,),
        ).fetchall()
        ids = set()
        for r in mrows:
            ids.update([r["winner_id"], r["loser_id"]])
        ids |= set(champs.values())
        names = {}
        for pid in ids:
            if pid is None:
                continue
            row = conn.execute("SELECT name FROM players WHERE player_id=?", (pid,)).fetchone()
            names[pid] = row["name"] if row else f"#{pid}"
        matches = []
        for r in mrows:
            if r["winner_id"] is None or r["loser_id"] is None:
                continue
            matches.append({
                "event": r["event"], "stage": r["stage"], "dayName": r["day_name"],
                "date": r["match_date"], "court": r["court"],
                "winnerId": r["winner_id"], "winnerName": names.get(r["winner_id"]),
                "loserId": r["loser_id"], "loserName": names.get(r["loser_id"]),
                "score": build_score(conn, r["match_id"], player_is_first=(r["p1_id"] == r["winner_id"])),
            })
        for c in cats:
            cid = champs.get(c["event"])
            c["championId"] = cid
            c["championName"] = names.get(cid) if cid else None
        return {"tournament": dict(t), "club": dict(club) if club else None,
                "categories": cats, "matches": matches}

    def api_clubs(self, conn, q):
        where, params = ["1=1"], []
        if q.get("q"):
            where.append(f"{foldsql('c.name')} LIKE ?"); params.append(f"%{fold(q['q'])}%")
        limit = min(int(q.get("limit", 100)), 600)
        sql = f"""
            SELECT c.club_id, c.name, c.city,
                   (SELECT count(*) FROM players p WHERE p.club_id=c.club_id) AS player_count,
                   (SELECT count(*) FROM player_ratings pr WHERE pr.club_id=c.club_id) AS rated_count
            FROM clubs c WHERE {' AND '.join(where)}
            ORDER BY player_count DESC LIMIT ?
        """
        return {"clubs": rows_to_dicts(conn.execute(sql, (*params, limit)).fetchall())}

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
