"""Import the full Apple Health export (export.zip from the Health app).

Every quantity record is kept in ``apple_records``. Weight, body composition
and nutrition are also rolled up per day into ``daily`` (source "apple"),
so the history from Zepp Life and Yazio shows up next to the Garmin data.
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path
from typing import IO, Iterator
from xml.etree.ElementTree import iterparse

from . import store

log = logging.getLogger(__name__)

PREFIX = "HKQuantityTypeIdentifier"

# Apple type (without prefix) -> (our field, combine, factor)
DAILY = {
    "BodyMass": ("weight_kg", "last", 1),
    "BodyFatPercentage": ("body_fat_pct", "last", 100),
    "LeanBodyMass": ("lean_mass_kg", "last", 1),
    "BodyMassIndex": ("bmi", "last", 1),
    "DietaryEnergyConsumed": ("kcal_in", "sum", 1),
    "DietaryProtein": ("protein_g", "sum", 1),
    "DietaryCarbohydrates": ("carbs_g", "sum", 1),
    "DietaryFatTotal": ("fat_g", "sum", 1),
    "DietaryFiber": ("fiber_g", "sum", 1),
    "DietaryWater": ("water_ml", "sum", 1),
}


def _open(path: str) -> IO[bytes]:
    if path.endswith(".zip"):
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.endswith("export.xml") and "cda" not in n)
        return z.open(name)
    return open(path, "rb")


def records(path: str) -> Iterator[dict[str, str]]:
    with _open(path) as f:
        for _event, el in iterparse(f, events=("end",)):
            if el.tag == "Record" and el.get("type", "").startswith(PREFIX):
                yield dict(el.attrib)
            el.clear()


def import_export(db_path: str, path: str) -> int:
    con = store.connect(db_path)
    per_day: dict[str, dict[str, float]] = {}
    n = 0
    batch = []
    for r in records(path):
        try:
            value = float(r.get("value", ""))
        except ValueError:
            continue
        kind = r["type"][len(PREFIX):]
        start = r.get("startDate", "")
        batch.append((kind, r.get("sourceName", ""), r.get("unit"), start, r.get("endDate"), value))
        spec = DAILY.get(kind)
        if spec and len(start) >= 10:
            field, how, factor = spec
            unit = (r.get("unit") or "").lower()
            if field == "weight_kg" and unit == "lb":
                value *= 0.45359237
            if field == "kcal_in" and unit == "kj":
                value /= 4.184
            if field == "water_ml" and unit == "l":
                value *= 1000
            row = per_day.setdefault(start[:10], {})
            v = value * factor
            row[field] = v if how == "last" or field not in row else row[field] + v
        if len(batch) >= 5000:
            con.executemany("INSERT OR IGNORE INTO apple_records VALUES (?, ?, ?, ?, ?, ?)", batch)
            con.commit()
            n += len(batch)
            batch = []
    con.executemany("INSERT OR IGNORE INTO apple_records VALUES (?, ?, ?, ?, ?, ?)", batch)
    con.commit()
    n += len(batch)
    # Note: records arrive in export order (by type, then time), so "last" is the latest of the day.
    for day, fields in per_day.items():
        store.save_daily(con, day, "apple", {k: round(v, 2) for k, v in fields.items()})
    con.close()
    log.info("Apple Health: %d records, %d days with weight/nutrition", n, len(per_day))
    return n


def find_export(inbox: Path) -> list[Path]:
    return sorted(p for p in inbox.glob("*") if p.suffix in (".zip", ".xml"))
