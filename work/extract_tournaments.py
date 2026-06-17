from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]

SOURCE = Path(
    "/Users/bahadiruner/.codex/attachments/738db062-fe20-43e4-87d8-64b3c31d44f9/pasted-text.txt"
)
OUTPUT = PROJECT_ROOT / "outputs" / "tournaments.json"

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


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.split()).strip()


def extract_id(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/(\d+)(?:[/?#].*)?$", url)
    return match.group(1) if match else None


def split_date_and_week(value: str) -> tuple[str, int | None]:
    match = re.search(r"\((\d+)\)\s*$", value)
    if not match:
        return value, None
    date = clean_text(value[: match.start()])
    return date, int(match.group(1))


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
            class_names = (attributes.get("class") or "").split()
            if "tab-pane" in class_names:
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


def selected_year(block: str) -> int:
    match = re.search(r'<option value="(\d{4})" selected(?:="")?>', block)
    if not match:
        raise ValueError("Selected year could not be found in block")
    return int(match.group(1))


def parse_block(block: str) -> list[dict[str, Any]]:
    year = selected_year(block)
    parser = TournamentTableParser()
    parser.feed(block)

    tournaments: list[dict[str, Any]] = []
    for pane_id, rows in parser.rows_by_pane.items():
        if pane_id not in TAB_KEYS:
            continue

        for row in rows:
            if len(row) < 5:
                continue

            date, week = split_date_and_week(row[1]["text"])
            tournament_url = row[0]["href"]
            club_url = row[2]["href"]

            tournaments.append(
                {
                    "year": year,
                    "tab": TAB_KEYS[pane_id],
                    "tabLabel": TAB_LABELS[pane_id],
                    "turnuvaAdi": row[0]["text"],
                    "turnuvaUrl": tournament_url,
                    "turnuvaId": extract_id(tournament_url),
                    "tarih": date,
                    "hafta": week,
                    "kulupAdi": row[2]["text"],
                    "kulupUrl": club_url,
                    "kulupId": extract_id(club_url),
                    "yer": row[3]["text"],
                    "kategori": row[4]["text"],
                }
            )

    return tournaments


def main() -> None:
    html = SOURCE.read_text(encoding="utf-8", errors="ignore")
    starts = [m.start() for m in re.finditer(r'<h2 class="txt-b35-008f-ls105">Turnuvalar</h2>', html)]
    starts.append(len(html))

    tournaments: list[dict[str, Any]] = []
    for index in range(len(starts) - 1):
        tournaments.extend(parse_block(html[starts[index] : starts[index + 1]]))

    payload = {"tournaments": tournaments}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for tournament in tournaments:
        key = f"{tournament['year']}:{tournament['tab']}"
        counts[key] = counts.get(key, 0) + 1

    print(json.dumps({"total": len(tournaments), "counts": counts}, ensure_ascii=False, indent=2))
    print(OUTPUT)


if __name__ == "__main__":
    main()
