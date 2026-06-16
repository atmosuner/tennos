# Tennos — Junior Tennis Analytics

Scrapes İ-KORT (ikort.com.tr) Turkish junior tennis (8–12 yaş) data, resolves player
clubs, computes Elo ratings, and serves a browsable web app — **Python standard library
only**, no third-party backend deps.

The web app runs fully static: `work/web/index.html` loads a SQLite database
(`tennos-web.db.gz`, ~10 MB) into the browser and runs every query client-side with
[sql.js](https://sql.js.org). It is published to GitHub Pages.

## Stats in this snapshot

- ~30.9k players · 539 clubs · 552 tournaments · ~83k matches (~74.7k scored)
- Elo ratings for ~9.5k players, split by gender and age category

## Pipeline (each step idempotent / resumable)

```bash
python3 work/extract_tournaments.py
python3 work/scrape_tournament_details.py
python3 work/scrape_clubs.py
python3 work/resolve_clubs.py        # match "(ABBREV)" -> club, per player, via profiles
python3 work/scrape_players.py       # player directory by birth year
python3 work/build_db.py             # -> outputs/tennos.db
python3 work/build_ratings.py        # Elo -> player_ratings
python3 work/build_web_db.py && gzip -9 -f work/web/tennos-web.db   # -> shippable browser DB
```

## Run locally

```bash
python3 work/serve.py        # http://localhost:8001  (live backend)
# or fully static:
cd work/web && python3 -m http.server 8080   # http://localhost:8080 (sql.js, no backend)
```

`index.html` works both ways: its `api()` calls hit `serve.py` locally or the in-browser
SQLite when served statically. See `work/web/DEPLOY.md`.

Note: `outputs/` and the raw HTML caches are git-ignored (regenerable by re-running the
pipeline). The committed `work/web/tennos-web.db.gz` is the data snapshot the site uses.
