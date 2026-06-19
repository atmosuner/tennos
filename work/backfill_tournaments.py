"""Backfill older İ-KORT tournament years into the tournament lists.

The public turnuvalar page accepts a year via the GET form field `selectYears`,
e.g. https://www.ikort.com.tr/turnuvalar?selectYears=2020 . This fetches each
year in a range, parses the "geçmiş" pane, keeps 8–14 yaş singles (same filter
the rest of the pipeline uses), and merges the new tournaments — deduped by
turnuvaId — into both:

    outputs/tournaments.json               (full list)
    outputs/filtered_yas_tournaments.json  (what scrape_tournament_details reads)

It does NOT scrape match details; after backfilling, run the normal pipeline:

    python3 work/scrape_tournament_details.py   # resumable; fetches only new ids
    python3 work/resolve_clubs.py               # incremental
    python3 work/build_db.py
    python3 work/build_ratings.py
    python3 work/build_web_db.py
    gzip -9 -f work/web/tennos-web.db

Usage:
    python3 work/backfill_tournaments.py --dry-run --no-ssl-verify   # preview
    python3 work/backfill_tournaments.py --no-ssl-verify             # write

Flags:
    --start / --end   year range, inclusive (default 2018–2023)
    --dry-run         print the plan, write nothing
    --no-ssl-verify   required on SSL-inspecting corporate proxies
    --sleep           seconds between year fetches (default 1.0)
"""

from __future__ import annotations

import argparse
import json
import ssl
import time
from pathlib import Path
from typing import Any

import refresh_current as rc

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOURNAMENTS = PROJECT_ROOT / "outputs" / "tournaments.json"
FILTERED = PROJECT_ROOT / "outputs" / "filtered_yas_tournaments.json"
BASE_URL = "https://www.ikort.com.tr"


def load_list(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not path.exists():
        return {"tournaments": []}, []
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("tournaments", payload if isinstance(payload, list) else [])
    return payload, items


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill older tournament years.")
    ap.add_argument("--start", type=int, default=2018)
    ap.add_argument("--end", type=int, default=2023)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-ssl-verify", action="store_true")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    ssl_ctx = None
    if args.no_ssl_verify:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    tourn_payload, tourn_list = load_list(TOURNAMENTS)
    filt_payload, filt_list = load_list(FILTERED)
    tourn_ids = {str(t.get("turnuvaId")) for t in tourn_list if t.get("turnuvaId")}
    filt_ids = {str(t.get("turnuvaId")) for t in filt_list if t.get("turnuvaId")}

    new_tourn: list[dict[str, Any]] = []
    new_filt: list[dict[str, Any]] = []

    for year in range(args.start, args.end + 1):
        url = f"{BASE_URL}/turnuvalar?selectYears={year}"
        print(f"fetch {url} …", end=" ", flush=True)
        try:
            html = rc.fetch_html(url, timeout=args.timeout, ssl_context=ssl_ctx)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {exc}")
            continue
        rows = rc.parse_tournaments_html(html)
        # Past tournaments only; force the requested year (page's selected option drives it).
        past = [t for t in rows if t.get("tab") == "gecmis" and t.get("turnuvaId")]
        for t in past:
            t["year"] = year
        yas = [t for t in past if rc.is_yas_tournament(t)]
        add_t = [t for t in yas if str(t["turnuvaId"]) not in tourn_ids]
        add_f = [t for t in yas if str(t["turnuvaId"]) not in filt_ids]
        for t in add_t:
            tourn_ids.add(str(t["turnuvaId"]))
            new_tourn.append(t)
        for t in add_f:
            filt_ids.add(str(t["turnuvaId"]))
            new_filt.append(t)
        print(f"geçmiş={len(past)}, yaş={len(yas)}, yeni(tüm liste)={len(add_t)}, yeni(filtered)={len(add_f)}")
        time.sleep(args.sleep)

    print(f"\nTOPLAM yeni turnuva: tournaments.json +{len(new_tourn)}, filtered +{len(new_filt)}")
    if not new_filt and not new_tourn:
        print("Eklenecek yeni turnuva yok.")
        return 0

    if args.dry_run:
        print("\n[dry-run] hiçbir şey yazılmadı. Örnek yeni kayıtlar:")
        for t in (new_filt or new_tourn)[:8]:
            print(f"  {t['year']} · {t['turnuvaId']} · {t['turnuvaAdi']} · {t.get('yer','')}")
        return 0

    tourn_list.extend(new_tourn)
    filt_list.extend(new_filt)
    tourn_payload["tournaments"] = tourn_list
    filt_payload["tournaments"] = filt_list
    rc.atomic_write_json(TOURNAMENTS, tourn_payload)
    rc.atomic_write_json(FILTERED, filt_payload)
    print(f"\nyazıldı → {TOURNAMENTS.name} ({len(tourn_list)}), {FILTERED.name} ({len(filt_list)})")
    print("Sıradaki: scrape_tournament_details.py → resolve_clubs.py → build_db.py → build_ratings.py → build_web_db.py + gzip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
