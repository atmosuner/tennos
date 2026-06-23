# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scrapes İ-KORT (ikort.com.tr) Turkish junior tennis (8–14 yaş) data, resolves player
clubs, computes Elo ratings, and serves a web app — all with **Python standard library
only** (no third-party deps). Single source of truth is `outputs/`; `tennos.db` and the
web app are derived views.

## Pipeline (run in order; each step is idempotent / resumable)

```bash
python3 work/extract_tournaments.py          # pasted HTML dump -> outputs/tournaments.json
python3 work/scrape_tournament_details.py    # -> outputs/tournament_details/{id}.json (805 tournaments, ~103k matches)
python3 work/scrape_clubs.py                 # -> outputs/clubs.json + club_abbrev_map.json
python3 work/resolve_clubs.py                # match "(ABBREV)" -> club via player profiles -> player_club_overrides.json
python3 work/scrape_players.py               # player directory by birth year -> outputs/players.json (~31k players)
python3 work/scrape_klasman_puan.py          # İ-KORT klasman puanı history -> klasman_puan table
python3 work/build_db.py                     # all JSON -> outputs/tennos.db (SQLite)
python3 work/build_ratings.py                # Elo -> player_ratings table in tennos.db
python3 work/serve.py                        # web app at http://localhost:8001
```

`scrape_players.py` only covers birth years ~2008–2018. For players whose last match
predates that window, run the one-time backfill to fill `birth_year`/`gender` from each
profile page (idempotent — only touches rows where `gender IS NULL`):

```bash
python3 work/backfill_legacy_player_profiles.py   # --dry-run / --workers N / --limit N
```

After the scraper adds new tournaments, re-run `resolve_clubs.py` (incremental — only
fetches newly-unresolved players), then `build_db.py` + `build_ratings.py` to refresh.

### Incremental refresh (ongoing tournaments)

```bash
python3 work/refresh_current.py --no-ssl-verify
```

Fetches ikort.com.tr/turnuvalar, re-scrapes active yaş turnuvaları, rebuilds DB + web DB
in one shot. Run manually when tournament results need updating.

**Source union:** scrapes two sets merged:
1. ikort "güncel" tab right now (new + ongoing)
2. `tennos.db` tournaments with `source_tab='guncel'` (previously current, may have moved
   to geçmiş on ikort before we ran the script)

**Filters:** 8–14 yaş only (checks `turnuvaAdi`); drops doubles/takım, drops 15+ yaş.

**Flags:** `--dry-run` (print plan, no writes), `--no-rebuild` (scrape only), `--no-gzip`,
`--no-ssl-verify` (required on corporate proxy networks with SSL inspection).

## Key concepts

**Club resolution is the hard part.** Player names in match data carry a free-text club
abbreviation, e.g. `ELA ANDIÇ (GTA)`. The same abbreviation can mean different clubs
(`ATA` → 2 clubs, `KSK` → Karşıyaka *and* Kayseri), so resolution is **per-player, not
per-abbreviation**. Resolution order (`resolve_clubs.py` + `build_db.py::resolve_club`):
1. `FERDI` → unaffiliated, club stays null.
2. abbrev in `club_abbrev_map.json` unique set → trusted club id (no fetch).
3. playerId in `player_club_overrides.json` → resolved club (from the player's own
   profile page `kulup-detay` link — the definitive per-player source).

**The public `/kulupler` list (286) is incomplete.** Real club ids go to ~927; many
clubs only surface via player profiles, rosters, or the player directory. `clubs.json`
(539) merges all sources, each tagged with a `source` field.

**Elo** (`build_ratings.py`): single pool (so playing up an age group is rewarded),
chronological by match date, provisional K=40 for first 30 matches then K=20, only
`result_type='completed'` matches. `age_group` per player is the highest age group they
have played in; rankings give `overall_rank`, `age_group_rank`, and `gender_rank`.
When the rankings API is called with `age_group` filter, `wins`/`losses` are computed
from matches in that age group only (not career totals).

**Rankings list (`/api/rankings`) is driven from the `players` table**, LEFT JOINing
`player_ratings` — so unrated players (no Elo yet) still appear, sorted last. Filters
(`club_id`, `gender`, `birth_year`, `city`, `q`) hit `players` columns, not `pr.*`.
`active_only` is **opt-in** (last 6 months); default shows everyone. Each row also carries
`club_abbrev` (from `clubs.abbrev`) and `kp` (latest İ-KORT klasman puanı).

**`clubs.abbrev`** (populated by `build_db.py::populate_club_abbrev`): trusted unique
abbrev from `club_abbrev_map.json` first, else the most common non-FERDI abbrev seen in
that club's match data. Used as the inline club tag in rankings.

**klasman puanı** (`scrape_klasman_puan.py` → `klasman_puan` table): official İ-KORT
ranking points, keyed by `player_id, year, week, type`. Rankings show the latest value;
player profiles chart the history.

**match_id** is `md5(tournament|dayId|court|matchCode|rawText)[:16]` — deterministic, so
rebuilds are idempotent.

## Web app

`work/serve.py` is a stdlib `http.server` JSON API + static SPA (`work/web/index.html`,
vanilla JS, hash routing). Endpoints: `/api/stats`, `/api/rankings`, `/api/player/{id}`,
`/api/h2h/{a}/{b}`, `/api/common/{a}/{b}`, `/api/players`, `/api/tournaments`,
`/api/tournament/{id}`, `/api/clubs`, `/api/search`. Opens DB read-only. Port 8001.

**`index.html` always uses `dbApi` (in-browser sql.js against `tennos-web.db.gz`).** The
`serve.py` JSON API endpoints exist for direct testing (curl etc.) but the UI never hits
them — `const api=p=>window.dbApi(p)` is hardcoded. Keep the two in sync: any endpoint
change in `serve.py` must be mirrored in the `dbApi` block, and vice versa.
`work/build_web_db.py` produces the slimmed browser DB; see `work/web/DEPLOY.md`.
After data changes: run `build_web_db.py` + `gzip -9 -f work/web/tennos-web.db` to
refresh the browser DB.

**Cache-busting:** `bootDB` fetches `tennos-web.db.gz?v=<DB_SCHEMA_VER>`. When the web
DB shape changes (new column/table), **bump `DB_SCHEMA_VER`** in that fetch — otherwise
returning visitors run the new SQL against a browser-cached old DB and get
`no such column ...`. Data-only refreshes (no schema change) don't need a bump.

Score rendering: `sets.p1/p2` follow the match's player order, not winner-first. `matches`
stores `p1_id`/`p2_id`; `build_score` orients the score by whether the viewer is `p1_id`
(passing `won` instead is the classic bug — it reverses scores for losers shown as p1).
`build_set_score` gives the winner-perspective set count ("2-1") used in compare/h2h.

Tournament detail (`/api/tournament/{id}`) reports per-event `championId`/`championName`
**and** `finalistId`/`finalistName` (champion = winner of a `%Final%` match, finalist =
its loser). Each match also carries `winnerRating`/`loserRating` — each player's Elo
*as of just before that match* (from `player_rating_history`), not their current rating.

## Data shapes

- Tournament detail JSON: `tournament` (fields, rawFields, notes), `groups`,
  `matchSchedule[].matches[]` (players with scores/sets, `result.winner/loser` by id).
- Field names are Turkish camelCase; `FIELD_KEYS` in `scrape_tournament_details.py` maps
  Turkish labels → normalized keys.
- `players.json`: playerId, name, birthYear, gender, clubId (name-matched), city.

## Auth note

Player profiles show only birth *year* anonymously; full birth date needs a logged-in
session cookie (`work/ikort_cookies.txt`, gitignore it — session cookies expire).
Not currently required by the pipeline.
