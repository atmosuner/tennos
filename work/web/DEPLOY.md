# Tennos — GitHub Pages deploy

The web app is fully static: `index.html` loads SQLite databases into the browser and
runs every query client-side with **sql.js** (SQLite compiled to WebAssembly, fetched
from a CDN). There is no backend to host.

## Files served (everything in `work/web/`)

- `index.html` — the single-page app (vanilla JS, hash routing)
- `tennos-web.db.gz` — "core" db (~2 MB as of 2026-06): clubs, players, tournaments,
  player_ratings, and a `web_precomputed` table holding the home page's stats/insights
  (computed once in `build_web_db.py`, not via live queries). Fetched eagerly by
  `bootDB` on every page load — this is what makes first paint fast.
- `tennos-web-detail.db.gz` — "detail" db (~46 MB as of 2026-06, grows with data):
  matches, sets, klasman_puan, player_rating_history, plus a self-contained copy of the
  core tables (sql.js has no ATTACH-from-bytes support, so cross-file joins aren't
  possible — pages needing match-level data query this file exclusively instead).
  Fetched lazily by `window.ensureDetail()` on first navigation to rankings, a player
  profile, a tournament detail page, h2h, or compare — see the `NEEDS_DETAIL` set in
  `index.html`'s `dbApi` dispatcher.
- `.nojekyll` — tells GitHub Pages to skip Jekyll processing

Both `.db` files have their non-PK indexes stripped before gzipping and rebuilt
client-side right after load (`CORE_INDEX_SQL` / `DETAIL_INDEX_SQL` in the `bootDB`
block) — cuts each download by ~35% at the cost of a sub-second `CREATE INDEX` pass.

`sql.js` (JS + `.wasm`) is loaded from cdnjs at runtime, so it is not committed.

## Rebuild the web database (after re-scraping)

```bash
python3 work/build_db.py          # rebuild outputs/tennos.db
python3 work/build_ratings.py     # Elo ratings
python3 work/build_web_db.py      # -> work/web/tennos-web.db + tennos-web-detail.db
gzip -9 -f work/web/tennos-web.db work/web/tennos-web-detail.db
```

If `index.html`'s SQL changed (new column, new table, new index), bump
`DB_SCHEMA_VER` in the `bootDB` block so cached visitors re-download instead of
running new SQL against an old cached DB.

`build_web_db.py` also re-derives `web_precomputed` (home page stats) from the SQL in
`index.html`'s `ep.stats()`/`ep.homeInsights()` — if you change that SQL, mirror the
change in `compute_stats()`/`compute_home_insights()` in `build_web_db.py` too.

## Publish to GitHub Pages

```bash
# from a clean copy of work/web/ as the site root
cd work/web
git init -b main
git add index.html tennos-web.db.gz tennos-web-detail.db.gz .nojekyll
git commit -m "Tennos static site"
git remote add origin git@github.com:<user>/<repo>.git
git push -u origin main
```

Then in the repo: **Settings → Pages → Source = `main` / root**. Site appears at
`https://<user>.github.io/<repo>/`.

Notes:
- Both `.gz` files are well under GitHub's 100 MB file limit. First load downloads only
  the ~2 MB core file; the ~46 MB detail file is fetched on first need and the browser
  caches both afterwards.
- Hash routing (`#/player/123`) needs no SPA fallback — Pages serves `index.html` at the
  root and the client handles routes.
- To update data, rebuild both `.gz` files and push them; visitors get the new DB on
  next load (or next detail-page visit, for the detail file).
