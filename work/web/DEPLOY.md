# Tennos — GitHub Pages deploy

The web app is fully static: `index.html` loads `tennos-web.db.gz` into the browser and
runs every query client-side with **sql.js** (SQLite compiled to WebAssembly, fetched
from a CDN). There is no backend to host.

## Files served (everything in `work/web/`)

- `index.html` — the single-page app (vanilla JS, hash routing)
- `tennos-web.db.gz` — gzipped SQLite database (~10 MB), decompressed in the browser
- `.nojekyll` — tells GitHub Pages to skip Jekyll processing

`sql.js` (JS + `.wasm`) is loaded from cdnjs at runtime, so it is not committed.

## Rebuild the web database (after re-scraping)

```bash
python3 work/build_db.py          # rebuild outputs/tennos.db
python3 work/build_ratings.py     # Elo ratings
python3 work/build_web_db.py      # -> work/web/tennos-web.db (slimmed)
gzip -9 -f work/web/tennos-web.db # -> work/web/tennos-web.db.gz
```

`build_web_db.py` drops what the frontend never queries (match_players, groups,
matches.raw_text) and VACUUMs, shrinking 60 MB → ~33 MB → ~10 MB gzipped.

## Publish to GitHub Pages

```bash
# from a clean copy of work/web/ as the site root
cd work/web
git init -b main
git add index.html tennos-web.db.gz .nojekyll
git commit -m "Tennos static site"
git remote add origin git@github.com:<user>/<repo>.git
git push -u origin main
```

Then in the repo: **Settings → Pages → Source = `main` / root**. Site appears at
`https://<user>.github.io/<repo>/`.

Notes:
- The `.gz` is 10 MB — well under GitHub's 100 MB file limit. First load downloads it once;
  the browser caches it afterwards.
- Hash routing (`#/player/123`) needs no SPA fallback — Pages serves `index.html` at the
  root and the client handles routes.
- To update data, rebuild the `.gz` and push it; visitors get the new DB on next load.
