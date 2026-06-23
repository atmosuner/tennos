# Tennos — Junior Tennis Analytics

Scrapes İ-KORT (ikort.com.tr) Turkish junior tennis (8–14 yaş) data, resolves player
clubs, computes Elo ratings, and serves a browsable web app — **Python standard library
only**, no third-party backend deps. Branded **Kortex**.

The web app runs fully static: `work/web/index.html` loads a SQLite database
(`tennos-web.db.gz`) into the browser and runs every query client-side with
[sql.js](https://sql.js.org). It is published to GitHub Pages.

## Stats in this snapshot

- ~36.9k players · 557 clubs · 2,353 tournaments · ~313k matches (~278k scored)
- Elo ratings for ~22.6k players, split by gender and age category
- İ-KORT klasman puanı (official ranking points) history merged in

## Pipeline (each step idempotent / resumable)

```bash
python3 work/extract_tournaments.py
python3 work/scrape_tournament_details.py
python3 work/scrape_clubs.py
python3 work/resolve_clubs.py        # match "(ABBREV)" -> club, per player, via profiles
python3 work/scrape_players.py       # player directory by birth year
python3 work/scrape_klasman_puan.py  # İ-KORT klasman puanı -> klasman_puan
python3 work/build_db.py             # -> outputs/tennos.db
python3 work/build_ratings.py        # Elo -> player_ratings
python3 work/build_web_db.py && gzip -9 -f work/web/tennos-web.db   # -> shippable browser DB
```

One-time backfill for players that predate the directory scrape window:

```bash
python3 work/backfill_legacy_player_profiles.py   # fills birth_year/gender from profile pages
```

## Run locally

```bash
python3 work/serve.py        # http://localhost:8001  (live backend)
# or fully static:
cd work/web && python3 -m http.server 8080   # http://localhost:8080 (sql.js, no backend)
```

`index.html` always queries the in-browser SQLite (`dbApi`); `serve.py`'s JSON API mirrors
the same endpoints for direct testing (curl etc.). See `work/web/DEPLOY.md`.

Note: `outputs/` and the raw HTML caches are git-ignored (regenerable by re-running the
pipeline). The committed `work/web/tennos-web.db.gz` is the data snapshot the site uses.
