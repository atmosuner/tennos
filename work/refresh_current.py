"""Fetch current (güncel) tournaments from ikort, re-scrape them, rebuild the web DB.

Run after tournaments have started to pick up new match results:

    python3 work/refresh_current.py
    python3 work/refresh_current.py --dry-run
    python3 work/refresh_current.py --no-rebuild   # scrape only, skip DB steps
    python3 work/refresh_current.py --no-gzip      # skip gzip (use when gzip not on PATH)
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


BASE_URL = "https://www.ikort.com.tr"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = PROJECT_ROOT / "outputs"
WORK = PROJECT_ROOT / "work"
TOURNAMENTS_JSON = OUTPUTS / "tournaments.json"
FILTERED_JSON = OUTPUTS / "filtered_yas_tournaments.json"
DB_PATH = OUTPUTS / "tennos.db"
WEB_DB = WORK / "web" / "tennos-web.db"
WEB_DB_GZ = WORK / "web" / "tennos-web.db.gz"
WEB_DETAIL_DB = WORK / "web" / "tennos-web-detail.db"
WEB_DETAIL_DB_GZ = WORK / "web" / "tennos-web-detail.db.gz"

TAB_KEYS = {
    "guncelturnuvalar": "guncel",
    "guncelturnuvalarpast": "gecmis",
    "club18yasalti": "club18YasAlti",
}
TAB_LABELS = {
    "guncelturnuvalar": "Güncel",
    "guncelturnuvalarpast": "Geçmiş",
    "club18yasalti": "18 Yaş Altı Türkiye Takımlar Şampiyonası",
}
GUNCEL_PANE = "guncelturnuvalar"


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split()).strip()


def extract_id(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"/(\d+)(?:[/?#].*)?$", url)
    return m.group(1) if m else None


def split_date_and_week(value: str) -> tuple[str, int | None]:
    m = re.search(r"\((\d+)\)\s*$", value)
    if not m:
        return value, None
    return clean_text(value[: m.start()]), int(m.group(1))


class TournamentTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.current_pane: str | None = None
        self.div_depth = 0
        self.pane_stack: list[tuple[str, int]] = []
        self.in_tbody = False
        self.in_tr = False
        self.current_cell: dict[str, Any] | None = None
        self.current_row: list[dict[str, Any]] = []
        self.rows_by_pane: dict[str, list[list[dict[str, Any]]]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "div":
            self.div_depth += 1
            if "tab-pane" in (attributes.get("class") or "").split():
                pane_id = attributes.get("id") or ""
                self.current_pane = pane_id
                self.pane_stack.append((pane_id, self.div_depth))
                self.rows_by_pane.setdefault(pane_id, [])
        if tag == "tbody":
            self.in_tbody = True
        if tag == "tr" and self.in_tbody:
            self.in_tr = True
            self.current_row = []
        if tag in {"th", "td"} and self.in_tr:
            self.current_cell = {"text": "", "href": None}
        if tag == "a" and self.current_cell is not None:
            self.current_cell["href"] = attributes.get("href")

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self.current_cell is not None:
            self.current_cell["text"] = clean_text(self.current_cell["text"])
            self.current_row.append(self.current_cell)
            self.current_cell = None
        if tag == "tr" and self.in_tr:
            if self.current_pane and self.current_row:
                self.rows_by_pane.setdefault(self.current_pane, []).append(self.current_row)
            self.in_tr = False
            self.current_row = []
        if tag == "tbody":
            self.in_tbody = False
        if tag == "div":
            if self.pane_stack and self.pane_stack[-1][1] == self.div_depth:
                self.pane_stack.pop()
                self.current_pane = self.pane_stack[-1][0] if self.pane_stack else None
            self.div_depth -= 1


def selected_year(html: str) -> int | None:
    m = re.search(r'<option value="(\d{4})" selected(?:="")?>', html)
    return int(m.group(1)) if m else None


def parse_tournaments_html(html: str) -> list[dict[str, Any]]:
    starts = [m.start() for m in re.finditer(r'<h2 class="txt-b35-008f-ls105">Turnuvalar</h2>', html)]
    if not starts:
        starts = [0]
    starts.append(len(html))

    results: list[dict[str, Any]] = []
    for i in range(len(starts) - 1):
        block = html[starts[i]: starts[i + 1]]
        year = selected_year(block)
        parser = TournamentTableParser()
        parser.feed(block)
        for pane_id, rows in parser.rows_by_pane.items():
            if pane_id not in TAB_KEYS:
                continue
            for row in rows:
                if len(row) < 5:
                    continue
                date, week = split_date_and_week(row[1]["text"])
                t_url = row[0]["href"]
                c_url = row[2]["href"]
                results.append({
                    "year": year,
                    "tab": TAB_KEYS[pane_id],
                    "tabLabel": TAB_LABELS[pane_id],
                    "turnuvaAdi": row[0]["text"],
                    "turnuvaUrl": t_url,
                    "turnuvaId": extract_id(t_url),
                    "tarih": date,
                    "hafta": week,
                    "kulupAdi": row[2]["text"],
                    "kulupUrl": c_url,
                    "kulupId": extract_id(c_url),
                    "yer": row[3]["text"],
                    "kategori": row[4]["text"],
                })
    return results


def fetch_html(url: str, timeout: float = 30.0, ssl_context: ssl.SSLContext | None = None) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.6",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="ignore")


def is_yas_tournament(t: dict[str, Any]) -> bool:
    """Keep 8-14 yaş individual tournaments; drop 7 yaş, 15+ yaş, and doubles."""
    text = f"{t.get('turnuvaAdi', '')} {t.get('kategori', '')}".lower()
    if re.search(r"(çift|cift|double|takım|takim)", text):
        return False
    m = re.search(r"(\d+)[- \d]*\s*ya[sş]", text)
    if not m:
        return False
    return 8 <= int(m.group(1)) <= 14


def get_db_guncel_ids() -> set[str]:
    """Return tournament IDs from DB that were previously seen as 'guncel' and pass yaş filter."""
    if not DB_PATH.exists():
        return set()
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT tournament_id, name FROM tournaments WHERE source_tab='guncel'"
        ).fetchall()
        conn.close()
        return {str(tid) for tid, name in rows if is_yas_tournament({"turnuvaAdi": name})}
    except Exception:
        return set()


def load_existing(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("tournaments", data if isinstance(data, list) else [])
    return {str(t["turnuvaId"]): t for t in items if t.get("turnuvaId")}


def _gzip_file(src: Path, dst: Path) -> None:
    with src.open("rb") as f_in, gzip.open(dst, "wb", compresslevel=9) as f_out:
        shutil.copyfileobj(f_in, f_out)
    print(f"  {dst} ({dst.stat().st_size / 1e6:.1f} MB)")


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def run(cmd: list[str], dry_run: bool) -> bool:
    print(f"  $ {' '.join(cmd)}")
    if dry_run:
        return True
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, env=env)
    return result.returncode == 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Fetch güncel tournaments and refresh web DB.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned actions, make no changes.")
    parser.add_argument("--no-rebuild", action="store_true", help="Skip build_db / build_ratings / build_web_db steps.")
    parser.add_argument("--no-gzip", action="store_true", help="Skip gzip step (use when gzip not on PATH).")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout for list page fetch.")
    parser.add_argument("--no-ssl-verify", action="store_true", help="Disable SSL certificate verification (for corporate proxies).")
    args = parser.parse_args()

    ssl_ctx: ssl.SSLContext | None = None
    if args.no_ssl_verify:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        print("WARNING: SSL verification disabled", file=sys.stderr)

    # 1. Fetch tournament list
    url = f"{BASE_URL}/turnuvalar"
    print(f"fetch {url} …", end=" ", flush=True)
    try:
        html = fetch_html(url, timeout=args.timeout, ssl_context=ssl_ctx)
    except Exception as exc:
        print(f"HATA: {exc}", file=sys.stderr)
        return 1
    print("ok")

    # 2. Parse
    fetched = parse_tournaments_html(html)
    guncel = [t for t in fetched if t.get("tab") == "guncel" and t.get("turnuvaId")]
    print(f"parsed: {len(fetched)} total, {len(guncel)} güncel")

    if not guncel:
        print("güncel turnuva yok — çıkılıyor")
        return 0

    # 3. Merge into tournaments.json
    existing = load_existing(TOURNAMENTS_JSON)
    before = len(existing)
    for t in fetched:
        tid = str(t.get("turnuvaId") or "")
        if tid:
            existing[tid] = t
    merged = list(existing.values())
    if not args.dry_run:
        atomic_write_json(TOURNAMENTS_JSON, {"tournaments": merged})
    print(f"tournaments.json: {before} -> {len(merged)} (+{len(merged)-before} yeni)")

    # 4. Update filtered_yas_tournaments.json
    yas_existing = load_existing(FILTERED_JSON)
    yas_new = {str(t["turnuvaId"]): t for t in fetched if t.get("turnuvaId") and is_yas_tournament(t)}
    yas_merged = {**yas_existing, **yas_new}
    if not args.dry_run:
        atomic_write_json(FILTERED_JSON, {"tournaments": list(yas_merged.values())})
    guncel_yas = [t for t in guncel if is_yas_tournament(t)]
    ikort_ids = {str(t["turnuvaId"]) for t in guncel_yas}
    db_ids = get_db_guncel_ids()
    only_in_db = db_ids - ikort_ids
    all_ids = ikort_ids | db_ids
    print(f"filtered_yas: ikort güncel={len(ikort_ids)}, db güncel={len(db_ids)}, sadece db'de={len(only_in_db)}, toplam={len(all_ids)}")

    if not all_ids:
        print("yaş kategorisinde güncel turnuva yok — çıkılıyor")
        return 0

    for t in guncel_yas:
        print(f"  {t['turnuvaId']}  {t['turnuvaAdi']}  [{t['kategori']}]  {t['tarih']}")
    if only_in_db:
        print(f"  + DB'den eklenen: {sorted(only_in_db)}")

    ids = ",".join(sorted(all_ids))

    if args.no_rebuild:
        print("\n--no-rebuild: scrape de atlandı (sadece liste güncellendi)")
        return 0

    # 5. Re-scrape güncel tournaments
    print(f"\n[scrape] {len(guncel_yas)} turnuva --refresh --force")
    scrape_cmd = [sys.executable, str(WORK / "scrape_tournament_details.py"),
                  "--only-id", ids, "--refresh", "--force"]
    if args.no_ssl_verify:
        scrape_cmd.append("--no-ssl-verify")
    ok = run(scrape_cmd, args.dry_run)
    if not ok:
        print("scrape başarısız", file=sys.stderr)
        return 1

    # 6. Rebuild DB + ratings
    print("\n[build_db]")
    if not run([sys.executable, str(WORK / "build_db.py")], args.dry_run):
        return 1
    print("\n[build_ratings]")
    if not run([sys.executable, str(WORK / "build_ratings.py")], args.dry_run):
        return 1

    # 7. Build web DB
    print("\n[build_web_db]")
    if not run([sys.executable, str(WORK / "build_web_db.py")], args.dry_run):
        return 1

    # 8. Gzip (Python stdlib fallback — no external gzip binary needed)
    if not args.no_gzip:
        print("\n[gzip]")
        if not args.dry_run:
            _gzip_file(WEB_DB, WEB_DB_GZ)
            if WEB_DETAIL_DB.exists():
                _gzip_file(WEB_DETAIL_DB, WEB_DETAIL_DB_GZ)
        else:
            print(f"  gzip {WEB_DB} -> {WEB_DB_GZ}")
            print(f"  gzip {WEB_DETAIL_DB} -> {WEB_DETAIL_DB_GZ}")
    else:
        print("\n[gzip atlandı — --no-gzip]")

    if not args.dry_run and WEB_DB_GZ.exists():
        mb = WEB_DB_GZ.stat().st_size / 1e6
        print(f"\ntennos-web.db.gz güncellendi ({mb:.1f} MB)")
        if WEB_DETAIL_DB_GZ.exists():
            mb2 = WEB_DETAIL_DB_GZ.stat().st_size / 1e6
            print(f"tennos-web-detail.db.gz güncellendi ({mb2:.1f} MB)")
        print("git add work/web/tennos-web.db.gz work/web/tennos-web-detail.db.gz && git commit && git push")

    print("\ndone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
