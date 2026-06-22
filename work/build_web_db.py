"""Produce a slimmed copy of tennos.db for shipping to the browser (sql.js).

Drops what the web frontend never queries: the match_players and groups tables,
and the large matches.raw_text column. Then VACUUMs to reclaim space.

    python3 work/build_web_db.py   ->  web/tennos-web.db
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "outputs" / "tennos.db"
DST = Path(__file__).resolve().parent / "web" / "tennos-web.db"


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"DB yok: {SRC}")
    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SRC, DST)
    conn = sqlite3.connect(DST)
    cur = conn.cursor()
    cur.executescript("DROP TABLE IF EXISTS match_players; DROP TABLE IF EXISTS groups;")
    cols = [r[1] for r in cur.execute("PRAGMA table_info(matches)").fetchall()]
    if "raw_text" in cols:
        cur.execute("ALTER TABLE matches DROP COLUMN raw_text")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    mb = DST.stat().st_size / 1e6
    print(f"web db -> {DST} ({mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
