"""Weight and nutrition from the Google Sheet the iPhone app HealthFit keeps.

HealthFit writes Apple Health data (Zepp Life scale, Yazio) into a sheet with
tabs "Gewicht" and "Ernährung". We download it with rclone as .xlsx (works
with any file id, also outside the remote's root folder) and store one row per
day as source "healthfit". Free alternative to Health Auto Export.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import store

log = logging.getLogger(__name__)

# tab -> {column header: (field, scale)}
TABS = {
    "Gewicht": {"Gewicht": "weight_kg", "Fett": "body_fat_pct", "BMI": "bmi"},
    "Ernährung": {"Nahrungsenergie": "kcal_in", "Protein": "protein_g", "Kohlenhydrate": "carbs_g",
                  "Gesamtfettanteil": "fat_g"},
}
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def _day(v: Any) -> str | None:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", str(v or "").strip())
    if m:
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(v or ""))
    return m[0] if m else None


def _num(v: Any, field: str) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if field == "body_fat_pct" and x <= 1:  # stored as fraction with % format
            x *= 100
        return x
    s = str(v)
    m = _NUM.search(s.replace(" ", "").replace(" ", "") if "%" in s else s)
    if not m:
        return None
    x = float(m[0].replace(",", "."))
    if field == "weight_kg" and "lb" in s:
        x *= 0.45359237
    if field == "kcal_in" and "kj" in s.lower():
        x /= 4.184
    return x


def parse_rows(tab: str, rows: list[tuple]) -> dict[str, dict[str, float]]:
    """``rows`` incl. header row -> {day: {field: value}}."""
    if not rows:
        return {}
    header = [str(h or "").strip() for h in rows[0]]
    cols = {i: TABS[tab][h] for i, h in enumerate(header) if h in TABS[tab]}
    out: dict[str, dict[str, float]] = {}
    for r in rows[1:]:
        day = _day(r[0] if r else None)
        if not day:
            continue
        for i, field in cols.items():
            v = _num(r[i] if i < len(r) else None, field)
            if v is not None:
                out.setdefault(day, {})[field] = round(v, 2)
    return out


def parse_xlsx(path: str) -> dict[str, dict[str, float]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    days: dict[str, dict[str, float]] = {}
    for tab in TABS:
        if tab not in wb.sheetnames:
            continue
        for day, fields in parse_rows(tab, list(wb[tab].iter_rows(values_only=True))).items():
            days.setdefault(day, {}).update(fields)
    return days


def download(sheet_id: str, dest: Path) -> None:
    remote = os.getenv("RCLONE_REMOTE", "gdrive:").split(":")[0] + ":"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rclone", "backend", "copyid", remote, sheet_id, str(dest),
                    "--drive-export-formats", "xlsx"], check=True, capture_output=True, text=True)


def sync(db_path: str, data_dir: Path) -> int:
    sheet_id = os.getenv("HEALTHFIT_SHEET_ID")
    if not sheet_id:
        return 0
    path = data_dir / "healthfit.xlsx"
    download(sheet_id, path)
    days = parse_xlsx(str(path))
    con = store.connect(db_path)
    for day, fields in days.items():
        store.save_daily(con, day, "healthfit", fields)
    con.close()
    log.info("HealthFit sheet: %d days", len(days))
    return len(days)
