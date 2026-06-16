from __future__ import annotations

import argparse
import html
import http.cookiejar
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_URL = "https://www.ikort.com.tr"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = PROJECT_ROOT / "outputs" / "filtered_yas_tournaments.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "tournament_details"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "work" / "ikort_cache"
DEFAULT_MANIFEST = PROJECT_ROOT / "outputs" / "tournament_details_manifest.json"


FIELD_KEYS = {
    "Kategori": "kategori",
    "Oynanacak Kort Tipi": "kortTipi",
    "Bölge Tipi": "bolgeTipi",
    "Seri Tipi": "seriTipi",
    "Yeri": "yer",
    "Oynanacak Zemin Türü": "zemin",
    "Kulüp": "kulup",
    "Ayrılan Kort Sayısı": "kortSayisi",
    "Ek Hizmetler": "ekHizmetler",
    "Eleme Tarihi": "elemeTarihi",
    "Tarihi": "baslangicTarihi",
    "Bitiş Tarihi": "bitisTarihi",
    "Kayit Kabul Başlangıç": "kayitKabulBaslangic",
    "Son Kayıt Tarihi": "sonKayitTarihi",
    "Geri Çekilme": "geriCekilme",
    "Turnuva Direktörü": "turnuvaDirektoru",
    "Başhakem": "bashakem",
    "Gözlem Hakemi": "gozlemHakemi",
    "Kule Hakemi": "kuleHakemi",
    "Çizgi Hakemi": "cizgiHakemi",
}


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<script.*?</script>|<style.*?</style>", " ", value, flags=re.S | re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split()).strip()


def strip_tags(value: str | None) -> str:
    return clean_text(value)


def absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    return urllib.parse.urljoin(BASE_URL, html.unescape(url))


def extract_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/(\d+)(?:[/?#].*)?$", url)
    return match.group(1) if match else None


def int_or_none(value: str | int | None) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group(0)) if match else None


def number_or_text(value: str | None) -> int | str | None:
    value = clean_text(value)
    if value == "":
        return None
    return int(value) if re.fullmatch(r"\d+", value) else value


def normalize_time(value: str | None) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    return re.sub(r"\s+", "", value).replace(".", ":").strip(":") or None


def is_bye_player(player: dict[str, Any]) -> bool:
    name = clean_text(player.get("name"))
    return bool(re.fullmatch(r"(?:M\s+Galibi|Bye)", name, flags=re.I))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    return f"{int(minutes)}m {remaining_seconds:.1f}s"


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value, encoding="utf-8", errors="ignore")
    tmp.replace(path)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def parse_input_tournaments(path: Path) -> list[dict[str, Any]]:
    payload = load_json(path, {})
    tournaments = payload.get("tournaments", payload if isinstance(payload, list) else [])
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in tournaments:
        tournament_id = str(item.get("turnuvaId") or item.get("tournamentId") or "").strip()
        if not tournament_id or tournament_id in seen:
            continue
        seen.add(tournament_id)
        unique.append(item)
    return unique


@dataclass
class FetchResult:
    url: str
    text: str
    from_cache: bool
    status: int | None


class IkortClient:
    def __init__(
        self,
        cache_dir: Path,
        delay: float,
        jitter: float,
        timeout: float,
        retries: int,
        refresh: bool,
    ) -> None:
        self.cache_dir = cache_dir
        self.delay = delay
        self.jitter = jitter
        self.timeout = timeout
        self.retries = retries
        self.refresh = refresh
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self.last_network_at = 0.0
        self.cache_hits = 0
        self.network_fetches = 0

    def sleep_if_needed(self) -> None:
        wait = self.delay + random.uniform(0, self.jitter)
        elapsed = time.monotonic() - self.last_network_at
        if self.last_network_at and elapsed < wait:
            time.sleep(wait - elapsed)

    def fetch(
        self,
        url: str,
        cache_path: Path,
        *,
        method: str = "GET",
        form: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_cache: bool = True,
    ) -> FetchResult:
        if use_cache and not self.refresh and cache_path.exists():
            self.cache_hits += 1
            return FetchResult(url=url, text=cache_path.read_text(encoding="utf-8", errors="ignore"), from_cache=True, status=None)

        body = None
        request_headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.7,en;q=0.6",
        }
        if headers:
            request_headers.update(headers)
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
            request_headers.setdefault("X-Requested-With", "XMLHttpRequest")

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.sleep_if_needed()
            request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    text = raw.decode(charset, errors="ignore")
                    self.last_network_at = time.monotonic()
                    self.network_fetches += 1
                    atomic_write_text(cache_path, text)
                    return FetchResult(url=url, text=text, from_cache=False, status=response.status)
            except urllib.error.HTTPError as exc:
                self.last_network_at = time.monotonic()
                last_error = exc
                if exc.code == 429:
                    retry_after = int_or_none(exc.headers.get("Retry-After")) or int((attempt + 1) * max(self.delay, 5))
                    time.sleep(retry_after)
                elif 500 <= exc.code < 600 and attempt < self.retries:
                    time.sleep((attempt + 1) * max(self.delay, 1))
                else:
                    raise
            except urllib.error.URLError as exc:
                self.last_network_at = time.monotonic()
                last_error = exc
                if attempt < self.retries:
                    time.sleep((attempt + 1) * max(self.delay, 1))
                else:
                    raise
        raise RuntimeError(f"Fetch failed for {url}: {last_error}")


def csrf_token(detail_html: str) -> str | None:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', detail_html)
    return html.unescape(match.group(1)) if match else None


def parse_detail_fields(detail_html: str) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_fields: dict[str, Any] = {}
    normalized: dict[str, Any] = {}

    blocks = re.split(r'<div class="col-md-4 mb-3">', detail_html)
    for block in blocks:
        label_match = re.search(r"<label[^>]*>(.*?)</label>", block, flags=re.S | re.I)
        if not label_match:
            continue
        label = strip_tags(label_match.group(1))
        if not label:
            continue

        value_area = block[label_match.end() :]
        value_divs_raw = re.findall(
            r'<div[^>]*class="[^"]*detailtab-div[^"]*"[^>]*>(.*?)</div>',
            value_area,
            flags=re.S | re.I,
        )
        values = [strip_tags(part) for part in value_divs_raw if strip_tags(part)]

        if value_divs_raw and not values:
            values = [""]
        elif not values:
            link_match = re.search(r"<a[^>]*>(.*?)</a>", value_area, flags=re.S | re.I)
            values = [strip_tags(link_match.group(1))] if link_match else [strip_tags(value_area)]
            values = [value for value in values if value]

        raw_fields[label] = values if len(values) > 1 else (values[0] if values else "")
        key = FIELD_KEYS.get(label)
        if key:
            normalized[key] = raw_fields[label]

        if label == "Kulüp":
            href_match = re.search(r'<a[^>]+href="([^"]+)"', value_area, flags=re.S | re.I)
            if href_match:
                url = absolute_url(href_match.group(1))
                normalized["kulup"] = {
                    "name": values[0] if values else "",
                    "url": url,
                    "id": extract_id(url),
                }

    if "kortSayisi" in normalized:
        normalized["kortSayisi"] = int_or_none(str(normalized["kortSayisi"]))

    return raw_fields, normalized


def parse_notes(detail_html: str) -> list[str]:
    notes_section = re.search(r"<h2[^>]*>\s*Notlar\s*</h2>(.*?)(?:</div>\s*</div>\s*</div>\s*<div|<div class=\"tabcontent)", detail_html, flags=re.S | re.I)
    source = notes_section.group(1) if notes_section else detail_html
    notes = []
    for note in re.findall(r"<p[^>]*>(.*?)</p>", source, flags=re.S | re.I):
        text = strip_tags(note)
        if text and text not in notes:
            notes.append(text)
    return notes


def parse_groups(detail_html: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for group_id, label in re.findall(r'<option value="(\d+)">\s*(.*?)\s*</option>', detail_html, flags=re.S | re.I):
        groups[group_id] = {
            "groupId": group_id,
            "name": strip_tags(label),
            "participantCount": None,
            "participantListUrl": f"{BASE_URL}/turnuvaya-katilanlar-listesi/{group_id}",
            "fixtureUrl": None,
            "fixtureText": None,
        }

    for row in re.findall(r"<tr>\s*(.*?)\s*</tr>", detail_html, flags=re.S | re.I):
        if "turnuvaya-katilanlar-listesi" not in row and "turnuvaya-fikstur" not in row:
            continue
        cells = re.findall(r"<(?:th|td)[^>]*>(.*?)</(?:th|td)>", row, flags=re.S | re.I)
        if len(cells) < 4:
            continue
        group_name = strip_tags(cells[0])
        participant_href = re.search(r'href="([^"]*turnuvaya-katilanlar-listesi(?:-ciftler)?/(\d+)[^"]*)"', row)
        fixture_href = re.search(r'href="([^"]*turnuvaya-fikstur[^"]*/(\d+)[^"]*)"', row)
        group_id = None
        if participant_href:
            group_id = participant_href.group(2)
        elif fixture_href:
            group_id = fixture_href.group(2)
        if not group_id:
            continue
        group = groups.setdefault(
            group_id,
            {
                "groupId": group_id,
                "name": group_name,
                "participantCount": None,
                "participantListUrl": None,
                "fixtureUrl": None,
                "fixtureText": None,
            },
        )
        group["name"] = group.get("name") or group_name
        if participant_href:
            group["participantListUrl"] = absolute_url(participant_href.group(1))
            group["participantCount"] = int_or_none(strip_tags(cells[2]))
        if fixture_href:
            group["fixtureUrl"] = absolute_url(fixture_href.group(1))
            group["fixtureText"] = strip_tags(cells[3])

    return list(groups.values())


def parse_days(detail_html: str) -> list[dict[str, Any]]:
    days = []
    pattern = re.compile(
        r'id="(tab-\d+)"[^>]*onclick="tournamentDetailMatchSchedule\(\s*\'[^\']+\'\s*,\s*(\d+)\s*\)"[^>]*>\s*([^<]+)\s*<span>([^<]+)</span>',
        flags=re.S | re.I,
    )
    for tab_id, day_id, day_name, date in pattern.findall(detail_html):
        days.append(
            {
                "tabId": tab_id,
                "dayId": day_id,
                "dayName": strip_tags(day_name),
                "date": strip_tags(date),
            }
        )
    return days


def parse_header(detail_html: str) -> dict[str, Any]:
    title_match = re.search(r'<h2 class="txt-r35-1d1d-ls15">(.*?)</h2>', detail_html, flags=re.S | re.I)
    date_match = re.search(r"Turnuva tarihi:\s*([^<]+)</div>", detail_html, flags=re.S | re.I)
    image_match = re.search(r'<img class="rounded-5" src="([^"]+)"', detail_html, flags=re.S | re.I)
    page_type_match = re.search(r'<h2 class="txt-b25-008f-ls075">\s*(.*?)\s*</h2>', detail_html, flags=re.S | re.I)
    name_match = re.search(r'<h2 class="txt-b25-008f-ls075 mb-4">(.*?)</h2>', detail_html, flags=re.S | re.I)
    return {
        "title": strip_tags(title_match.group(1)) if title_match else "",
        "dateRangeText": strip_tags(date_match.group(1)) if date_match else "",
        "imageUrl": absolute_url(image_match.group(1)) if image_match else None,
        "typeText": strip_tags(page_type_match.group(1)) if page_type_match else "",
        "name": strip_tags(name_match.group(1)) if name_match else "",
    }


def parse_participants(fragment_html: str, group: dict[str, Any]) -> list[dict[str, Any]]:
    participants = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", fragment_html, flags=re.S | re.I):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.S | re.I)
        if not cells or "Sonuç Bulunamadı" in strip_tags(row):
            continue
        texts = [strip_tags(cell) for cell in cells]
        link_match = re.search(r'href="([^"]*/oyuncu-profil/(\d+)[^"]*)"', row, flags=re.S | re.I)
        player_url = absolute_url(link_match.group(1)) if link_match else None
        participant: dict[str, Any] = {
            "groupId": group.get("groupId"),
            "groupName": group.get("name"),
            "rawCells": texts,
            "playerUrl": player_url,
            "playerId": extract_id(player_url),
        }
        if len(texts) >= 5:
            participant.update(
                {
                    "ks": int_or_none(texts[0]),
                    "puan": int_or_none(texts[1]),
                    "adSoyad": texts[2],
                    "kulup": texts[3],
                    "dogumTarihi": texts[4],
                }
            )
        participants.append(participant)
    return participants


def parse_fixture_page(fixture_html: str, group: dict[str, Any]) -> dict[str, Any]:
    section_names = []
    for section in re.findall(r'<div class="txt-b25-008f-ls075">(.*?)</div>', fixture_html, flags=re.S | re.I):
        text = strip_tags(section)
        if text.startswith("Grup") and text not in section_names:
            section_names.append(text)
    return {
        "groupId": group.get("groupId"),
        "groupName": group.get("name"),
        "sectionCount": len(section_names),
        "sections": section_names,
        "playerLinkCount": len(set(re.findall(r"/oyuncu-profil/\d+", fixture_html))),
    }


def parse_player_anchor(anchor_html: str) -> dict[str, Any]:
    href_match = re.search(r'href="([^"]*/oyuncu-profil/(\d+)[^"]*)"', anchor_html, flags=re.S | re.I)
    url = absolute_url(href_match.group(1)) if href_match else None
    return {
        "name": strip_tags(anchor_html),
        "playerUrl": url,
        "playerId": extract_id(url),
    }


def parse_score_cell(score_html: str) -> dict[str, Any]:
    tiebreak_match = re.search(
        r'<span[^>]*class="[^"]*matchs_tb_pos[^"]*"[^>]*>(.*?)</span>',
        score_html,
        flags=re.S | re.I,
    )
    tiebreak = strip_tags(tiebreak_match.group(1)) if tiebreak_match else ""
    without_tiebreak = re.sub(r"<span[^>]*>.*?</span>", " ", score_html, flags=re.S | re.I)
    games = strip_tags(without_tiebreak)
    raw = strip_tags(score_html)
    return {
        "games": number_or_text(games),
        "tiebreak": number_or_text(tiebreak),
        "raw": raw,
    }


def parse_score_cells(row_html: str) -> list[dict[str, Any]]:
    return [
        parse_score_cell(value)
        for value in re.findall(
            r'(<div class="position-relative[^"]*"[^>]*>.*?</div>)',
            row_html,
            flags=re.S | re.I,
        )
    ]


def is_winner_row(row_html: str) -> bool | None:
    name_anchor = re.search(r'<a[^>]*class="([^"]*)"[^>]*>', row_html, flags=re.S | re.I)
    name_div = re.search(
        r'<div class="(?!position-relative)([^"]*font-size-12[^"]*)"[^>]*>',
        row_html,
        flags=re.S | re.I,
    )
    class_text = " ".join(
        match.group(1)
        for match in (name_anchor, name_div)
        if match
    )
    if "txt-b15-008f" in class_text:
        return True
    if "txt-m15-5353" in class_text:
        return False
    return None


def parse_participant_rows(cell_html: str) -> list[dict[str, Any]]:
    row_segments = re.split(
        r'<div class="d-flex space-between align-items-center(?: mt-3 mb-3)?">',
        cell_html,
        flags=re.I,
    )
    participants = []
    for row_html in row_segments[1:]:
        name_anchor = re.search(r'<a[^>]+href="[^"]*/oyuncu-profil/\d+[^"]*"[^>]*>.*?</a>', row_html, flags=re.S | re.I)
        if name_anchor:
            participant = parse_player_anchor(name_anchor.group(0))
        else:
            name_match = re.search(
                r'<div class="(?!position-relative)[^"]*font-size-12[^"]*"[^>]*>(.*?)</div>',
                row_html,
                flags=re.S | re.I,
            )
            if not name_match:
                continue
            participant = {
                "name": strip_tags(name_match.group(1)),
                "playerUrl": None,
                "playerId": None,
            }

        score_details = parse_score_cells(row_html)
        participant["scores"] = [score["raw"] for score in score_details if score["raw"]]
        participant["scoreDetails"] = score_details
        row_winner = is_winner_row(row_html)
        if row_winner is not None:
            participant["isWinner"] = row_winner
        row_text = strip_tags(row_html)
        if "Walkover" in row_text:
            participant["status"] = "Walkover"
        elif re.search(r"\bRet\.?\b", row_text, flags=re.I):
            participant["status"] = "Retired"
        elif is_bye_player(participant):
            participant["status"] = "Bye"

        if participant["name"]:
            participant["isTeam"] = bool(re.search(r"\s-\s", participant["name"]))
            participants.append(participant)
    return participants


def build_sets(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(players) != 2:
        return []
    score_details = [player.get("scoreDetails", []) for player in players]
    max_sets = max((len(scores) for scores in score_details), default=0)
    sets = []
    for index in range(max_sets):
        p1_score = score_details[0][index] if index < len(score_details[0]) else None
        p2_score = score_details[1][index] if index < len(score_details[1]) else None
        sets.append(
            {
                "setNumber": index + 1,
                "type": "matchTiebreak" if index >= 2 else "set",
                "p1": p1_score,
                "p2": p2_score,
            }
        )
    return sets


def player_result(player: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": player.get("name"),
        "playerId": player.get("playerId"),
        "playerUrl": player.get("playerUrl"),
    }


def build_result(players: list[dict[str, Any]], sets: list[dict[str, Any]]) -> dict[str, Any]:
    if not players:
        return {"type": "placeholder", "winner": None, "loser": None}
    if len(players) != 2:
        return {"type": "unknown", "winner": None, "loser": None}

    walkover_players = [player for player in players if player.get("status") == "Walkover"]
    if len(walkover_players) == 2:
        return {
            "type": "doubleWalkover",
            "winner": None,
            "loser": None,
            "withdrawn": [player_result(player) for player in walkover_players],
        }
    if len(walkover_players) == 1:
        withdrawn = walkover_players[0]
        winner = players[1] if players[0] is withdrawn else players[0]
        return {
            "type": "walkover",
            "winner": player_result(winner),
            "loser": player_result(withdrawn),
            "withdrawn": player_result(withdrawn),
        }

    retired_players = [player for player in players if player.get("status") == "Retired"]
    if len(retired_players) == 1:
        retired = retired_players[0]
        winner = next((player for player in players if player.get("isWinner") is True), None)
        if winner is None or winner is retired:
            winner = players[1] if players[0] is retired else players[0]
        return {
            "type": "retirement",
            "winner": player_result(winner),
            "loser": player_result(retired),
            "retired": player_result(retired),
        }

    bye_players = [player for player in players if player.get("status") == "Bye" or is_bye_player(player)]
    if len(bye_players) == 2 and not sets:
        return {
            "type": "placeholder",
            "winner": None,
            "loser": None,
            "bye": [player_result(player) for player in bye_players],
        }
    if len(bye_players) == 1 and not sets:
        bye = bye_players[0]
        winner = next((player for player in players if player is not bye), None)
        if winner:
            return {
                "type": "bye",
                "winner": player_result(winner),
                "loser": None,
                "bye": player_result(bye),
            }

    winner = next((player for player in players if player.get("isWinner") is True), None)
    loser = next((player for player in players if player.get("isWinner") is False), None)
    if winner and loser:
        return {
            "type": "completed" if sets else "unknown",
            "winner": player_result(winner),
            "loser": player_result(loser),
        }

    return {
        "type": "completed" if sets else "scheduled",
        "winner": None,
        "loser": None,
    }


def split_event_stage(prefix: str) -> tuple[str, str]:
    prefix = clean_text(prefix)
    stage_match = re.search(
        r"\s(Haftasonu\s+Grup|Ana\s+Tablo|Eleme|Teselli|Final|Yarı\s+Final|Çeyrek\s+Final|Grup(?:\s+\S+)?)\s*$",
        prefix,
        flags=re.I,
    )
    if stage_match:
        return clean_text(prefix[: stage_match.start()]), clean_text(stage_match.group(1))
    return prefix, ""


def parse_match_cell(cell_html: str, court: str | None, day: dict[str, Any]) -> dict[str, Any] | None:
    text = strip_tags(cell_html)
    if not text:
        return None
    match_code_match = re.search(r"\b(M\d+(?:\s+G\d+\s*[AB])?)\b", text, flags=re.I)
    time_label_pattern = r"(?:BAŞLAMA(?:\s+SAAT[İI])?|EN\s+ERKEN(?:\s+SAAT)?|ERKEN\s+SAAT|SAAT|SAAY)"
    start_match = re.search(time_label_pattern + r"\s*:?\s*([0-9:.]+)", text, flags=re.I)
    players = parse_participant_rows(cell_html)

    if not players and "M" not in text:
        return None

    event_text = ""
    stage_text = ""
    first_player_name = players[0].get("name") if players else ""
    prefix = text.split(first_player_name, 1)[0] if first_player_name and first_player_name in text else text
    if not start_match and match_code_match:
        start_match = re.search(
            r"\bM\d+(?:\s+G\d+\s*[AB])?\s+([0-2]?\d\s*:\s*\d{2}|[0-2]?\d[.]\d{2})",
            prefix,
            flags=re.I,
        )
    prefix = re.sub(
        r"^.*?\bM\d+(?:\s+G\d+\s*[AB])?\s*(?:" + time_label_pattern + r"\s*:?\s*[0-9:.]+|M[Üü]teakip)\s*",
        "",
        prefix,
        flags=re.I,
    ).strip()
    age_match = re.search(r"\b(?:7|8|9|10|12|14|16|18)\s+Yaş\b", prefix, flags=re.I)
    if age_match:
        prefix = prefix[age_match.start() :]
    event_text, stage_text = split_event_stage(prefix)
    sets = build_sets(players)

    return {
        "dayId": day.get("dayId"),
        "dayName": day.get("dayName"),
        "date": day.get("date"),
        "court": court,
        "matchCode": clean_text(match_code_match.group(1)) if match_code_match else None,
        "startTime": normalize_time(start_match.group(1)) if start_match else None,
        "event": event_text,
        "stage": stage_text,
        "isDouble": any(player.get("isTeam") for player in players),
        "players": players,
        "sets": sets,
        "result": build_result(players, sets),
        "rawText": text,
    }


def parse_match_schedule(fragment_html: str, day: dict[str, Any]) -> dict[str, Any]:
    description_match = re.search(r'<p[^>]*class="[^"]*description[^"]*"[^>]*>(.*?)</p>', fragment_html, flags=re.S | re.I)
    courts = [strip_tags(value) for value in re.findall(r'<th class="txt-b25-008f-ls075">\s*(.*?)\s*</th>', fragment_html, flags=re.S | re.I)]
    matches = []
    rows = re.findall(r'<tr class="tr-likecolm">(.*?)</tr>', fragment_html, flags=re.S | re.I)
    for row in rows:
        cells = re.findall(r'<td class="td-likerow"[^>]*>(.*?)</td>', row, flags=re.S | re.I)
        for index, cell in enumerate(cells):
            parsed = parse_match_cell(cell, courts[index] if index < len(courts) else None, day)
            if parsed:
                matches.append(parsed)
    return {
        "dayId": day.get("dayId"),
        "dayName": day.get("dayName"),
        "date": day.get("date"),
        "description": strip_tags(description_match.group(1)) if description_match else "",
        "courts": courts,
        "matches": matches,
    }


def should_keep_match(match: dict[str, Any], *, include_7_yas: bool, include_doubles: bool) -> bool:
    haystack = " ".join(
        str(match.get(key) or "")
        for key in ("event", "stage")
    ).lower()
    if not include_7_yas and re.search(r"(?<!\d)7\s*yaş", haystack, flags=re.I):
        return False
    if not include_doubles and (
        match.get("isDouble")
        or re.search(r"(^|\s)(çift|cift|double)(\s|$)", haystack, flags=re.I)
    ):
        return False
    return True


def filter_schedule(
    schedule: dict[str, Any],
    *,
    include_7_yas: bool,
    include_doubles: bool,
) -> dict[str, Any]:
    schedule["matches"] = [
        match
        for match in schedule.get("matches", [])
        if should_keep_match(match, include_7_yas=include_7_yas, include_doubles=include_doubles)
    ]
    return schedule


def scrape_one(
    client: IkortClient,
    entry: dict[str, Any],
    output_dir: Path,
    cache_dir: Path,
    include_participants: bool,
    include_fixtures: bool,
    include_schedule: bool,
    include_7_yas: bool,
    include_doubles: bool,
) -> dict[str, Any]:
    tournament_id = str(entry.get("turnuvaId") or entry.get("tournamentId"))
    item_cache_dir = cache_dir / tournament_id
    detail_url = f"{BASE_URL}/turnuva-detay/{tournament_id}"
    detail = client.fetch(detail_url, item_cache_dir / "detail.html")
    token = csrf_token(detail.text)

    raw_fields, normalized_fields = parse_detail_fields(detail.text)
    groups = parse_groups(detail.text)
    days = parse_days(detail.text)

    payload: dict[str, Any] = {
        "scrapedAt": now_iso(),
        "source": {
            "detailUrl": detail_url,
            "fromCache": detail.from_cache,
            "inputEntry": entry,
        },
        "tournament": {
            "turnuvaId": tournament_id,
            **parse_header(detail.text),
            "fields": normalized_fields,
            "rawFields": raw_fields,
            "notes": parse_notes(detail.text),
        },
        "groups": groups,
        "participants": [],
        "fixtureGroups": [],
        "matchSchedule": [],
        "rawCacheFiles": {
            "detail": str(item_cache_dir / "detail.html"),
        },
    }

    if include_participants and token:
        for group in groups:
            group_id = group["groupId"]
            fragment = client.fetch(
                f"{BASE_URL}/home-filter-tournumant-participant-list",
                item_cache_dir / f"participants_{group_id}.html",
                method="POST",
                form={"tournamentid": tournament_id, "tournamentgroup": group_id},
                headers={"X-CSRF-TOKEN": token},
            )
            payload["participants"].extend(parse_participants(fragment.text, group))
            payload["rawCacheFiles"][f"participants_{group_id}"] = str(item_cache_dir / f"participants_{group_id}.html")

    if include_fixtures:
        for group in groups:
            fixture_url = group.get("fixtureUrl")
            if not fixture_url:
                continue
            group_id = group["groupId"]
            fixture = client.fetch(fixture_url, item_cache_dir / f"fixture_{group_id}.html")
            payload["fixtureGroups"].append(parse_fixture_page(fixture.text, group))
            payload["rawCacheFiles"][f"fixture_{group_id}"] = str(item_cache_dir / f"fixture_{group_id}.html")

    if include_schedule:
        for day in days:
            day_id = day["dayId"]
            schedule = client.fetch(
                f"{BASE_URL}/turnuva-detay-mac-programi?{urllib.parse.urlencode({'dayId': day_id})}",
                item_cache_dir / f"match_schedule_{day_id}.html",
            )
            payload["matchSchedule"].append(
                filter_schedule(
                    parse_match_schedule(schedule.text, day),
                    include_7_yas=include_7_yas,
                    include_doubles=include_doubles,
                )
            )
            payload["rawCacheFiles"][f"match_schedule_{day_id}"] = str(item_cache_dir / f"match_schedule_{day_id}.html")

    atomic_write_json(output_dir / f"{tournament_id}.json", payload)
    return payload


def select_work(tournaments: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = tournaments
    if args.only_id:
        ids = {item.strip() for item in args.only_id.split(",") if item.strip()}
        selected = [item for item in selected if str(item.get("turnuvaId")) in ids]
    if args.start_after:
        passed = False
        trimmed = []
        for item in selected:
            if passed:
                trimmed.append(item)
            elif str(item.get("turnuvaId")) == str(args.start_after):
                passed = True
        selected = trimmed
    if args.limit is not None:
        selected = selected[: args.limit]
    return selected


def update_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    manifest["updatedAt"] = now_iso()
    atomic_write_json(manifest_path, manifest)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape I-KORT tournament detail pages with resumable per-tournament output.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Input JSON containing a tournaments list.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Per-tournament JSON output directory.")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="Raw HTML cache directory.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST, help="Progress manifest path.")
    parser.add_argument("--limit", type=int, default=None, help="Scrape at most N tournaments.")
    parser.add_argument("--only-id", default="", help="Comma-separated tournament ids to scrape.")
    parser.add_argument("--start-after", default="", help="Resume input ordering after this tournament id.")
    parser.add_argument("--delay", type=float, default=2.0, help="Minimum delay between network requests.")
    parser.add_argument("--jitter", type=float, default=1.0, help="Random extra delay between network requests.")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Retry count for transient failures.")
    parser.add_argument("--force", action="store_true", help="Reprocess tournaments even if output JSON already exists.")
    parser.add_argument("--refresh", action="store_true", help="Refetch raw HTML even if cache files already exist.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned work and exit without network calls.")
    parser.add_argument("--with-participants", action="store_true", help="Also scrape participant AJAX endpoint. Off by default.")
    parser.add_argument("--with-fixtures", action="store_true", help="Also scrape fixture pages. Off by default.")
    parser.add_argument("--no-schedule", action="store_true", help="Skip match schedule day endpoints.")
    parser.add_argument("--include-7-yas", action="store_true", help="Keep 7 Yaş matches in schedule output. Dropped by default.")
    parser.add_argument("--include-doubles", action="store_true", help="Keep doubles/Çift matches in schedule output. Dropped by default.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    tournaments = parse_input_tournaments(args.input)
    selected = select_work(tournaments, args)
    pending = [
        item
        for item in selected
        if args.force or not (args.output_dir / f"{item.get('turnuvaId')}.json").exists()
    ]

    print(f"input={args.input}")
    print(f"loaded={len(tournaments)} selected={len(selected)} pending={len(pending)}")
    print(f"output_dir={args.output_dir}")
    print(f"cache_dir={args.cache_dir}")
    if args.dry_run:
        preview = [str(item.get("turnuvaId")) for item in pending[:20]]
        print("dry_run=true")
        print("first_pending_ids=" + ", ".join(preview))
        if len(pending) > len(preview):
            print(f"remaining_after_preview={len(pending) - len(preview)}")
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_json(args.manifest, {"createdAt": now_iso(), "items": {}})
    manifest.setdefault("items", {})
    manifest["input"] = str(args.input)
    manifest["outputDir"] = str(args.output_dir)
    manifest["cacheDir"] = str(args.cache_dir)
    manifest["totalLoaded"] = len(tournaments)

    client = IkortClient(
        cache_dir=args.cache_dir,
        delay=args.delay,
        jitter=args.jitter,
        timeout=args.timeout,
        retries=args.retries,
        refresh=args.refresh,
    )

    for index, item in enumerate(pending, start=1):
        tournament_id = str(item.get("turnuvaId"))
        tournament_name = clean_text(str(item.get("turnuvaAdi") or item.get("name") or ""))
        label = f"{tournament_id} {tournament_name}".strip()
        print(f"[{index}/{len(pending)}] scraping {label}", flush=True)
        started_at = now_iso()
        started_monotonic = time.monotonic()
        cache_before = client.cache_hits
        network_before = client.network_fetches
        try:
            payload = scrape_one(
                client,
                item,
                args.output_dir,
                args.cache_dir,
                include_participants=args.with_participants,
                include_fixtures=args.with_fixtures,
                include_schedule=not args.no_schedule,
                include_7_yas=args.include_7_yas,
                include_doubles=args.include_doubles,
            )
            elapsed_seconds = time.monotonic() - started_monotonic
            schedule_days = len(payload.get("matchSchedule", []))
            match_count = sum(len(day.get("matches", [])) for day in payload.get("matchSchedule", []))
            cache_delta = client.cache_hits - cache_before
            network_delta = client.network_fetches - network_before
            manifest["items"][tournament_id] = {
                "status": "ok",
                "startedAt": started_at,
                "finishedAt": now_iso(),
                "elapsedSeconds": round(elapsed_seconds, 3),
                "output": str(args.output_dir / f"{tournament_id}.json"),
                "groups": len(payload.get("groups", [])),
                "participants": len(payload.get("participants", [])),
                "fixtureGroups": len(payload.get("fixtureGroups", [])),
                "scheduleDays": schedule_days,
                "matches": match_count,
                "networkFetches": network_delta,
                "cacheHits": cache_delta,
            }
            print(
                "  ok "
                f"elapsed={format_duration(elapsed_seconds)} "
                f"days={schedule_days} matches={match_count} "
                f"network={network_delta} cache={cache_delta} "
                f"output={args.output_dir / f'{tournament_id}.json'}",
                flush=True,
            )
        except Exception as exc:
            elapsed_seconds = time.monotonic() - started_monotonic
            manifest["items"][tournament_id] = {
                "status": "error",
                "startedAt": started_at,
                "finishedAt": now_iso(),
                "elapsedSeconds": round(elapsed_seconds, 3),
                "networkFetches": client.network_fetches - network_before,
                "cacheHits": client.cache_hits - cache_before,
                "error": repr(exc),
            }
            update_manifest(args.manifest, manifest)
            print(
                f"  error elapsed={format_duration(elapsed_seconds)} "
                f"network={client.network_fetches - network_before} "
                f"cache={client.cache_hits - cache_before} "
                f"tournament_id={tournament_id}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue
        update_manifest(args.manifest, manifest)

    ok_count = sum(1 for item in manifest["items"].values() if item.get("status") == "ok")
    error_count = sum(1 for item in manifest["items"].values() if item.get("status") == "error")
    print(f"done ok={ok_count} errors={error_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
